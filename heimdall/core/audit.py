"""Append-only, hash-chained audit log.

Every row stores the hash of the row before it, so changing or removing any
row breaks the chain from that point on. The chain alone only proves internal
consistency: someone who can write to the database can recompute all of it.
Signed checkpoints published outside the database (see `anchor.py`) are what
make tampering detectable.
"""

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import rfc8785

GENESIS_HASH = "0" * 64


class EventType(StrEnum):
    ALERT_RECEIVED = "alert_received"
    ALERT_UNCONFIRMED = "alert_unconfirmed"
    STATE_TRANSITION = "state_transition"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    LLM_CALL = "llm_call"
    DIAGNOSIS = "diagnosis"
    DIAGNOSIS_REJECTED = "diagnosis_rejected"
    PROPOSAL = "proposal"
    POLICY_DECISION = "policy_decision"
    APPROVAL = "approval"
    ACTION_EXECUTED = "action_executed"
    VERIFICATION = "verification"
    ROLLBACK = "rollback"
    ESCALATION = "escalation"
    FREEZE = "freeze"
    ANCHOR = "anchor"


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    timestamp: str
    incident_id: str | None
    actor: str
    event_type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class ChainProblem:
    """The first place where the stored chain does not verify."""

    seq: int
    reason: str


def compute_hash(
    *,
    seq: int,
    timestamp: str,
    incident_id: str | None,
    actor: str,
    event_type: str,
    payload: dict[str, Any],
    prev_hash: str,
) -> str:
    """sha256(prev_hash || canonical JSON of the row without its hash).

    Canonical JSON is RFC 8785, so the same row always hashes the same way
    regardless of key order or number formatting.
    """
    row: dict[str, Any] = {
        "seq": seq,
        "timestamp": timestamp,
        "incident_id": incident_id,
        "actor": actor,
        "event_type": event_type,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(prev_hash.encode() + rfc8785.dumps(row)).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditLog:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = _utc_now) -> None:
        self._conn = conn
        self._clock = clock
        self._lock = threading.Lock()
        self._listeners: list[Callable[[AuditEvent], None]] = []

    def subscribe(self, listener: Callable[[AuditEvent], None]) -> None:
        """Call `listener` after every appended event (used for anchoring)."""
        self._listeners.append(listener)

    def append(
        self,
        event_type: EventType,
        payload: dict[str, Any],
        *,
        actor: str,
        incident_id: str | None = None,
    ) -> AuditEvent:
        # Canonicalizing first rejects payloads that cannot be hashed
        # reproducibly (NaN, non-string keys, arbitrary objects).
        payload_json = rfc8785.dumps(payload).decode()
        timestamp = self._clock().isoformat(timespec="microseconds")

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                last = self._conn.execute(
                    "SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                seq = last["seq"] + 1 if last else 1
                prev_hash = last["hash"] if last else GENESIS_HASH
                event = AuditEvent(
                    seq=seq,
                    timestamp=timestamp,
                    incident_id=incident_id,
                    actor=actor,
                    event_type=str(event_type),
                    payload=json.loads(payload_json),
                    prev_hash=prev_hash,
                    hash=compute_hash(
                        seq=seq,
                        timestamp=timestamp,
                        incident_id=incident_id,
                        actor=actor,
                        event_type=str(event_type),
                        payload=payload,
                        prev_hash=prev_hash,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO audit_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (seq, timestamp, incident_id, actor, event.event_type, payload_json,
                     prev_hash, event.hash),
                )  # fmt: skip
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

        for listener in self._listeners:
            listener(event)
        return event

    def events(self, incident_id: str | None = None) -> Iterator[AuditEvent]:
        query = "SELECT * FROM audit_log"
        args: tuple[Any, ...] = ()
        if incident_id is not None:
            query += " WHERE incident_id = ?"
            args = (incident_id,)
        for row in self._conn.execute(query + " ORDER BY seq", args):
            yield _from_row(row)

    def head(self) -> AuditEvent | None:
        row = self._conn.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        return _from_row(row) if row else None

    def get(self, seq: int) -> AuditEvent | None:
        row = self._conn.execute("SELECT * FROM audit_log WHERE seq = ?", (seq,)).fetchone()
        return _from_row(row) if row else None

    def verify_chain(self) -> ChainProblem | None:
        """Recompute every hash. Returns the first broken link, or None."""
        expected_seq = 1
        prev_hash = GENESIS_HASH
        for row in self._conn.execute("SELECT * FROM audit_log ORDER BY seq"):
            seq = row["seq"]
            if seq != expected_seq:
                return ChainProblem(expected_seq, f"row {expected_seq} is missing")
            if row["prev_hash"] != prev_hash:
                return ChainProblem(seq, "prev_hash does not match the previous row")
            try:
                payload = json.loads(row["payload_json"])
            except ValueError:
                return ChainProblem(seq, "payload is not valid JSON")
            recomputed = compute_hash(
                seq=seq,
                timestamp=row["timestamp"],
                incident_id=row["incident_id"],
                actor=row["actor"],
                event_type=row["event_type"],
                payload=payload,
                prev_hash=row["prev_hash"],
            )
            if recomputed != row["hash"]:
                return ChainProblem(seq, "hash does not match the row content")
            prev_hash = row["hash"]
            expected_seq += 1
        return None


def _from_row(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        seq=row["seq"],
        timestamp=row["timestamp"],
        incident_id=row["incident_id"],
        actor=row["actor"],
        event_type=row["event_type"],
        payload=json.loads(row["payload_json"]),
        prev_hash=row["prev_hash"],
        hash=row["hash"],
    )
