"""Human approval for actions the policy will not run on its own.

An approval authorizes one exact action on one exact version of its target.
The token is an HMAC over the action's hash, the target's resource version
and the expiry, so it cannot be reused for a different action, a changed
target, or after it expires. Nothing a workload or a commit can write creates
or approves one: only a request carrying the token does.
"""

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from heimdall.core.actions import idempotency_key
from heimdall.core.models import ActionProposal


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    VOID = "void"  # the target changed after the approval was requested


class ApprovalError(Exception):
    pass


@dataclass(frozen=True)
class Approval:
    id: str
    incident_id: str
    proposal: ActionProposal
    action_hash: str
    resource_version: str | None
    expires_at: datetime
    status: ApprovalStatus


class ApprovalStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        secret: bytes,
        *,
        expiry: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if len(secret) < 16:
            raise ValueError("the approval secret must be at least 16 bytes")
        self._conn = conn
        self._secret = secret
        self._expiry = expiry
        self._clock = clock

    def token(self, approval: Approval) -> str:
        message = "|".join(
            [approval.id, approval.action_hash, approval.resource_version or "",
             approval.expires_at.isoformat()]
        )  # fmt: skip
        return hmac.new(self._secret, message.encode(), hashlib.sha256).hexdigest()

    def request(
        self, incident_id: str, proposal: ActionProposal, resource_version: str | None
    ) -> Approval:
        approval = Approval(
            id=secrets.token_hex(8),
            incident_id=incident_id,
            proposal=proposal,
            action_hash=idempotency_key(incident_id, proposal),
            resource_version=resource_version,
            expires_at=self._clock() + self._expiry,
            status=ApprovalStatus.PENDING,
        )
        self._conn.execute(
            "INSERT INTO approvals (id, incident_id, proposal_json, action_hash,"
            " resource_version, expires_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                approval.id,
                incident_id,
                proposal.model_dump_json(exclude_none=True),
                approval.action_hash,
                resource_version,
                approval.expires_at.isoformat(),
                approval.status,
            ),
        )
        return approval

    def get(self, approval_id: str) -> Approval | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return _approval(row) if row else None

    def _set(self, approval_id: str, status: ApprovalStatus) -> Approval:
        self._conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?",
            (status, self._clock().isoformat(), approval_id),
        )
        updated = self.get(approval_id)
        assert updated is not None  # noqa: S101 - just updated
        return updated

    def authenticate(self, approval_id: str, token: str) -> Approval:
        """Return the approval if the token is right; used to show a human
        what they are being asked to approve."""
        approval = self.get(approval_id)
        if approval is None or not hmac.compare_digest(token, self.token(approval)):
            raise ApprovalError("invalid approval id or token")
        return approval

    def decide(self, approval_id: str, token: str, *, approve: bool) -> Approval:
        """Record a human's decision. Works once per approval."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            approval = self.get(approval_id)
            # The same error for "no such approval" and "wrong token", so the
            # endpoint cannot be used to find out which ids exist.
            if approval is None or not hmac.compare_digest(token, self.token(approval)):
                raise ApprovalError("invalid approval id or token")
            if approval.status is not ApprovalStatus.PENDING:
                raise ApprovalError(f"this approval is already {approval.status}")
            if self._clock() >= approval.expires_at:
                self._set(approval_id, ApprovalStatus.EXPIRED)
                self._conn.execute("COMMIT")
                raise ApprovalError("this approval has expired")
            decided = self._set(
                approval_id, ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            )
            self._conn.execute("COMMIT")
            return decided
        except ApprovalError:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def void(self, approval_id: str) -> Approval:
        return self._set(approval_id, ApprovalStatus.VOID)

    def expire_due(self) -> list[Approval]:
        """Mark pending approvals past their expiry as expired and return them."""
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE status = ? AND expires_at <= ?",
            (ApprovalStatus.PENDING, self._clock().isoformat()),
        ).fetchall()
        return [self._set(row["id"], ApprovalStatus.EXPIRED) for row in rows]


def _approval(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"],
        incident_id=row["incident_id"],
        proposal=ActionProposal.model_validate_json(row["proposal_json"]),
        action_hash=row["action_hash"],
        resource_version=row["resource_version"],
        expires_at=datetime.fromisoformat(row["expires_at"]),
        status=ApprovalStatus(row["status"]),
    )
