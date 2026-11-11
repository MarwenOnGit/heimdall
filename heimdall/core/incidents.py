"""Incidents: grouping alerts, and the durable incident state machine."""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from heimdall.core.audit import AuditLog, EventType
from heimdall.core.models import TERMINAL_STATES, Alert, Incident, IncidentState
from heimdall.tools.dependencies import DependencyGraph

S = IncidentState

# Every allowed move. Anything else is a bug and raises.
TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    S.OPEN: frozenset({S.INVESTIGATING, S.RESOLVED, S.ESCALATED, S.FAILED}),
    # Back to OPEN when an investigation was interrupted by a restart.
    S.INVESTIGATING: frozenset({S.DIAGNOSED, S.OPEN, S.ESCALATED, S.FAILED}),
    S.DIAGNOSED: frozenset({S.PLANNING, S.ESCALATED, S.RESOLVED, S.FAILED}),
    S.PLANNING: frozenset({S.AWAITING_APPROVAL, S.EXECUTING, S.ESCALATED, S.RESOLVED, S.FAILED}),
    S.AWAITING_APPROVAL: frozenset({S.EXECUTING, S.ESCALATED, S.FAILED}),
    S.EXECUTING: frozenset({S.VERIFYING, S.ROLLED_BACK, S.ESCALATED, S.FAILED}),
    S.VERIFYING: frozenset({S.MITIGATED, S.RESOLVED, S.ROLLED_BACK, S.PLANNING, S.FAILED}),
    S.MITIGATED: frozenset({S.RESOLVED, S.ESCALATED, S.FAILED}),
    S.ROLLED_BACK: frozenset({S.ESCALATED, S.FAILED}),
    S.RESOLVED: frozenset(),
    S.ESCALATED: frozenset(),
    S.FAILED: frozenset(),
}

FLAP_WINDOW = timedelta(minutes=15)
FLAP_THRESHOLD = 3


class InvalidTransition(Exception):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


class IncidentManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        audit: AuditLog,
        graph: DependencyGraph,
        *,
        window: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._conn = conn
        self._audit = audit
        self._graph = graph
        self._window = window
        self._clock = clock

    # ------------------------------------------------------------ alerts

    def ingest(self, alert: Alert, *, confirmed: bool) -> Incident | None:
        """Record an alert and attach it to an incident.

        An alert that Prometheus does not confirm is stored and audited but
        can never open or extend an incident. Returns the incident the alert
        now belongs to, if any.
        """
        now = self._clock()
        alert_summary = {
            "alertname": alert.alertname,
            "status": alert.status,
            "fingerprint": alert.fingerprint,
            "labels": alert.labels,
        }
        if not confirmed:
            self._store_alert(alert, now, confirmed=False, incident_id=None)
            self._audit.append(EventType.ALERT_UNCONFIRMED, alert_summary, actor="ingest")
            return None

        if alert.status == "resolved":
            incident = self._incident_for_fingerprint(alert.fingerprint)
        else:
            incident = self._matching_open_incident(alert, now)
            if incident is None:
                incident = self._open_incident(alert, now)
            else:
                self._extend_incident(incident, alert, now)
                incident = self.get(incident.id)

        incident_id = incident.id if incident else None
        self._store_alert(alert, now, confirmed=True, incident_id=incident_id)
        self._audit.append(
            EventType.ALERT_RECEIVED, alert_summary, actor="ingest", incident_id=incident_id
        )
        if incident and not incident.flapping and self._is_flapping(alert.fingerprint, now):
            self._conn.execute("UPDATE incidents SET flapping = 1 WHERE id = ?", (incident.id,))
            incident = self.get(incident.id)
        return incident

    def _store_alert(
        self, alert: Alert, now: datetime, *, confirmed: bool, incident_id: str | None
    ) -> None:
        self._conn.execute(
            "INSERT INTO alerts (received_at, fingerprint, status, alertname, labels_json,"
            " raw_json, confirmed, incident_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now.isoformat(),
                alert.fingerprint,
                alert.status,
                alert.alertname,
                json.dumps(alert.labels, sort_keys=True),
                alert.model_dump_json(by_alias=True),
                int(confirmed),
                incident_id,
            ),
        )

    def _is_flapping(self, fingerprint: str, now: datetime) -> bool:
        """True when the alert changed status more than FLAP_THRESHOLD times
        within FLAP_WINDOW."""
        rows = self._conn.execute(
            "SELECT status FROM alerts WHERE fingerprint = ? AND confirmed = 1"
            " AND received_at >= ? ORDER BY id",
            (fingerprint, (now - FLAP_WINDOW).isoformat()),
        ).fetchall()
        statuses = [r["status"] for r in rows]
        changes = sum(1 for a, b in zip(statuses, statuses[1:], strict=False) if a != b)
        return changes > FLAP_THRESHOLD

    # ------------------------------------------------------- correlation

    def _matching_open_incident(self, alert: Alert, now: datetime) -> Incident | None:
        """The open incident this alert belongs to, if there is one.

        An alert joins an incident when it arrives within the window of the
        incident's last alert and is related to it: same namespace and a
        service that is the same as, or directly connected to, one already in
        the incident. Alerts without a service (node alerts) relate to
        everything, because a failing node can affect any service.
        """
        cutoff = (now - self._window).isoformat()
        rows = self._conn.execute(
            "SELECT * FROM incidents WHERE last_alert_at >= ? ORDER BY opened_at", (cutoff,)
        ).fetchall()
        for row in rows:
            incident = _incident(row)
            if incident.state in TERMINAL_STATES and not incident.flapping:
                continue
            if self._fingerprint_in_incident(alert.fingerprint, incident.id):
                return incident
            if alert.service is None or not incident.services:
                return incident
            if alert.namespace != incident.namespace:
                continue
            if any(self._graph.connected(alert.service, s) for s in incident.services):
                return incident
        return None

    def _fingerprint_in_incident(self, fingerprint: str, incident_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM alerts WHERE fingerprint = ? AND incident_id = ? LIMIT 1",
                (fingerprint, incident_id),
            ).fetchone()
            is not None
        )

    def _incident_for_fingerprint(self, fingerprint: str) -> Incident | None:
        row = self._conn.execute(
            "SELECT i.* FROM incidents i JOIN alerts a ON a.incident_id = i.id"
            " WHERE a.fingerprint = ? ORDER BY a.id DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        return _incident(row) if row else None

    def _open_incident(self, alert: Alert, now: datetime) -> Incident:
        day = now.strftime("%Y_%m_%d")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE id LIKE ?", (f"inc_{day}_%",)
            ).fetchone()[0]
            incident = Incident(
                id=f"inc_{day}_{count + 1:04d}",
                state=S.OPEN,
                namespace=alert.namespace,
                services=[alert.service] if alert.service else [],
                opened_at=now,
                updated_at=now,
            )
            self._conn.execute(
                "INSERT INTO incidents (id, state, namespace, services_json, flapping,"
                " last_alert_at, opened_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    incident.id,
                    incident.state,
                    incident.namespace,
                    json.dumps(incident.services),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._audit.append(
            EventType.STATE_TRANSITION,
            {"from": None, "to": S.OPEN, "reason": f"alert {alert.alertname}"},
            actor="incident_manager",
            incident_id=incident.id,
        )
        return incident

    def _extend_incident(self, incident: Incident, alert: Alert, now: datetime) -> None:
        services = list(incident.services)
        if alert.service and alert.service not in services:
            services.append(alert.service)
        self._conn.execute(
            "UPDATE incidents SET services_json = ?, namespace = COALESCE(namespace, ?),"
            " last_alert_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(services), alert.namespace, now.isoformat(), now.isoformat(), incident.id),
        )

    # ----------------------------------------------------- state machine

    def get(self, incident_id: str) -> Incident:
        row = self._conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if row is None:
            raise KeyError(incident_id)
        return _incident(row)

    def all(self, *, active_only: bool = False) -> list[Incident]:
        rows = self._conn.execute("SELECT * FROM incidents ORDER BY opened_at").fetchall()
        incidents = [_incident(r) for r in rows]
        if active_only:
            incidents = [i for i in incidents if i.state not in TERMINAL_STATES]
        return incidents

    def transition(self, incident_id: str, to: IncidentState, *, reason: str) -> Incident:
        """Move an incident to a new state. Every move is an audit event."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.get(incident_id)
            if to not in TRANSITIONS[current.state]:
                raise InvalidTransition(f"{incident_id}: {current.state} -> {to} is not allowed")
            self._conn.execute(
                "UPDATE incidents SET state = ?, updated_at = ? WHERE id = ?",
                (to, self._clock().isoformat(), incident_id),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._audit.append(
            EventType.STATE_TRANSITION,
            {"from": current.state, "to": to, "reason": reason},
            actor="incident_manager",
            incident_id=incident_id,
        )
        return self.get(incident_id)

    def reconcile(self) -> list[Incident]:
        """Called once at startup. Incidents whose investigation was cut short
        by a restart go back to OPEN so they are investigated again. Returns
        every incident that still needs work."""
        for incident in self.all(active_only=True):
            if incident.state is S.INVESTIGATING:
                self.transition(incident.id, S.OPEN, reason="investigation interrupted by restart")
        return self.all(active_only=True)


def _incident(row: sqlite3.Row) -> Incident:
    return Incident(
        id=row["id"],
        state=IncidentState(row["state"]),
        namespace=row["namespace"],
        services=json.loads(row["services_json"]),
        flapping=bool(row["flapping"]),
        opened_at=datetime.fromisoformat(row["opened_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
