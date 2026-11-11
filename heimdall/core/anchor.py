"""Signed checkpoints of the audit chain, kept outside the database.

An anchor is the pair (seq, hash) of an audit row, signed with an Ed25519 key.
Anchors are published somewhere the database writer cannot rewrite (a git
repo, a chat channel). Verification then catches what the hash chain alone
cannot: a chain that was recomputed after an edit, or truncated, no longer
matches the anchors that were already published.
"""

import base64
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from heimdall.core.audit import AuditEvent, AuditLog, ChainProblem, EventType


@dataclass(frozen=True)
class Anchor:
    seq: int
    hash: str
    timestamp: str
    signature: str  # base64 Ed25519 signature over the canonical JSON of the other fields

    def signed_bytes(self) -> bytes:
        return rfc8785.dumps({"seq": self.seq, "hash": self.hash, "timestamp": self.timestamp})


class AnchorPublisher(Protocol):
    def publish(self, anchor: Anchor) -> None: ...


class FileAnchorPublisher:
    """Appends anchors to a JSON Lines file. For development and tests; in the
    cluster, anchors go to a separate git repository and to the webhook."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def publish(self, anchor: Anchor) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as f:
            f.write(json.dumps(asdict(anchor)) + "\n")


def read_anchor_file(path: Path) -> list[Anchor]:
    return [Anchor(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def generate_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} is not an Ed25519 private key")
    return key


def private_key_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def public_key_pem(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def load_public_key(pem: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 public key")
    return key


def sign(event: AuditEvent, key: Ed25519PrivateKey) -> Anchor:
    unsigned = Anchor(seq=event.seq, hash=event.hash, timestamp=event.timestamp, signature="")
    signature = base64.b64encode(key.sign(unsigned.signed_bytes())).decode()
    return Anchor(seq=event.seq, hash=event.hash, timestamp=event.timestamp, signature=signature)


class Anchorer:
    """Publishes an anchor every `every` events, and on demand."""

    def __init__(
        self,
        log: AuditLog,
        key: Ed25519PrivateKey,
        publishers: list[AnchorPublisher],
        every: int = 50,
    ) -> None:
        if every < 1:
            raise ValueError("every must be at least 1")
        self._log = log
        self._key = key
        self._publishers = publishers
        self._every = every
        self._since_anchor = 0
        log.subscribe(self._on_event)

    def _on_event(self, event: AuditEvent) -> None:
        if event.event_type == EventType.ANCHOR:
            return
        self._since_anchor += 1
        if self._since_anchor >= self._every:
            self.anchor_now()

    def anchor_now(self) -> Anchor | None:
        """Sign and publish the current chain head. Called at the end of each
        incident so a finished incident is always covered by an anchor."""
        head = self._log.head()
        if head is None:
            return None
        anchor = sign(head, self._key)
        for publisher in self._publishers:
            publisher.publish(anchor)
        self._since_anchor = 0
        self._log.append(
            EventType.ANCHOR, {"anchored_seq": anchor.seq, "hash": anchor.hash}, actor="heimdall"
        )
        return anchor


def verify(
    log: AuditLog, anchors: list[Anchor], public_key: Ed25519PublicKey
) -> ChainProblem | None:
    """Full verification: the chain itself, then every published anchor.

    Returns the first problem found, or None when everything checks out.
    """
    problem = log.verify_chain()
    if problem is not None:
        return problem
    for anchor in sorted(anchors, key=lambda a: a.seq):
        try:
            public_key.verify(base64.b64decode(anchor.signature), anchor.signed_bytes())
        except (InvalidSignature, ValueError):
            return ChainProblem(anchor.seq, "anchor signature is invalid")
        event = log.get(anchor.seq)
        if event is None:
            return ChainProblem(anchor.seq, "anchored row is missing: the log was truncated")
        if event.hash != anchor.hash:
            return ChainProblem(
                anchor.seq, "row does not match its published anchor: the log was rewritten"
            )
    return None
