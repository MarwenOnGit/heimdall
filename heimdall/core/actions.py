"""The record of actions: what was proposed, decided and done.

It is the source for the limits that span more than one proposal (how many
writes an incident has had, whether the same action just ran), for
idempotency across restarts, and for the kill switch.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import rfc8785

from heimdall.core.models import ActionProposal, ActionType

CLUSTER_WRITES = frozenset(
    {
        ActionType.RESTART_DEPLOYMENT,
        ActionType.ROLLBACK_DEPLOYMENT,
        ActionType.SCALE_DEPLOYMENT,
        ActionType.PATCH_RESOURCES,
    }
)


class ActionStatus(StrEnum):
    DRY_RUN = "dry_run"  # accepted by the API server in dry-run mode
    EXECUTED = "executed"
    RECORDED = "recorded"  # nothing to run: escalate, none, or a PR not yet implemented
    AWAITING_APPROVAL = "awaiting_approval"
    DENIED = "denied"
    REJECTED_BY_API = "rejected_by_api"  # RBAC or the admission policy said no
    CONFLICT = "conflict"  # the target changed between the policy check and the write
    FAILED = "failed"
    UNDONE = "undone"  # ran, did not fix the problem, and was reverted


# Statuses that count as "this action ran" for rate limits. Dry runs count,
# so a dry-run report shows what live mode would really have allowed.
RAN = (ActionStatus.DRY_RUN, ActionStatus.EXECUTED)


@dataclass(frozen=True)
class ActionRecord:
    incident_id: str
    type: str
    namespace: str | None
    name: str | None
    decision: str
    status: ActionStatus
    detail: str
    created_at: datetime
    # What the target looked like before the action, so it can be undone.
    previous: dict[str, Any] | None = None


def idempotency_key(incident_id: str, proposal: ActionProposal) -> str:
    """Identifies one specific action of one incident. The wording of the
    justification does not matter; what would be done does."""
    payload = {
        "incident_id": incident_id,
        **proposal.model_dump(
            mode="json", exclude_none=True, exclude={"justification", "expected_effect", "summary"}
        ),
    }
    return hashlib.sha256(rfc8785.dumps(payload)).hexdigest()


class ActionStore:
    def __init__(
        self, conn: sqlite3.Connection, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._conn = conn
        self._clock = clock

    def find(self, incident_id: str, proposal: ActionProposal) -> ActionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM actions WHERE idempotency_key = ?",
            (idempotency_key(incident_id, proposal),),
        ).fetchone()
        return _record(row) if row else None

    def record(
        self,
        incident_id: str,
        proposal: ActionProposal,
        *,
        decision: str,
        status: ActionStatus,
        detail: str,
        previous: dict[str, Any] | None = None,
    ) -> ActionRecord:
        """Store the outcome of a proposal. Recording the same action of the
        same incident again replaces the earlier outcome."""
        self._conn.execute(
            "INSERT INTO actions (incident_id, idempotency_key, type, namespace, name,"
            " proposal_json, decision, status, detail, previous_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (idempotency_key) DO UPDATE SET decision = excluded.decision,"
            " status = excluded.status, detail = excluded.detail, created_at = excluded.created_at,"
            " previous_json = COALESCE(excluded.previous_json, actions.previous_json)",
            (
                incident_id,
                idempotency_key(incident_id, proposal),
                proposal.type,
                proposal.namespace,
                proposal.name,
                proposal.model_dump_json(exclude_none=True),
                decision,
                status,
                detail,
                json.dumps(previous) if previous is not None else None,
                self._clock().isoformat(),
            ),
        )
        found = self.find(incident_id, proposal)
        assert found is not None  # noqa: S101 - just written
        return found

    def for_incident(self, incident_id: str) -> list[ActionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM actions WHERE incident_id = ? ORDER BY id", (incident_id,)
        ).fetchall()
        return [_record(r) for r in rows]

    def writes_so_far(self, incident_id: str) -> int:
        """Cluster writes that ran for this incident."""
        return sum(
            1
            for a in self.for_incident(incident_id)
            if a.type in CLUSTER_WRITES and a.status in RAN
        )

    def recent(self, window: timedelta = timedelta(minutes=30)) -> list[dict[str, Any]]:
        """Cluster writes that ran recently, across all incidents, in the shape
        the policy expects."""
        now = self._clock()
        rows = self._conn.execute(
            "SELECT * FROM actions WHERE created_at >= ? ORDER BY id",
            ((now - window).isoformat(),),
        ).fetchall()
        return [
            {
                "type": r.type,
                "namespace": r.namespace,
                "name": r.name,
                "incident_id": r.incident_id,
                "minutes_ago": int((now - r.created_at).total_seconds() // 60),
            }
            for r in map(_record, rows)
            if r.type in CLUSTER_WRITES and r.status in RAN
        ]

    # ----------------------------------------------------- kill switch

    def frozen(self) -> bool:
        row = self._conn.execute("SELECT value FROM settings WHERE key = 'frozen'").fetchone()
        return row is not None and row["value"] == "true"

    def set_frozen(self, frozen: bool) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES ('frozen', ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            ("true" if frozen else "false",),
        )


def _record(row: sqlite3.Row) -> ActionRecord:
    return ActionRecord(
        incident_id=row["incident_id"],
        type=row["type"],
        namespace=row["namespace"],
        name=row["name"],
        decision=row["decision"],
        status=ActionStatus(row["status"]),
        detail=row["detail"],
        created_at=datetime.fromisoformat(row["created_at"]),
        previous=json.loads(row["previous_json"]) if row["previous_json"] else None,
    )
