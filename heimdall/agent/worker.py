"""Background worker: picks up open incidents and investigates them."""

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from heimdall.agent.investigator import Investigation, Investigator
from heimdall.agent.pipeline import Pipeline
from heimdall.core.incidents import IncidentManager
from heimdall.core.models import Incident, IncidentState, RootCauseCategory

log = logging.getLogger("heimdall.worker")


def incident_alerts(conn: sqlite3.Connection, incident_id: str) -> list[dict[str, Any]]:
    """The distinct alerts of an incident, with their latest status."""
    rows = conn.execute(
        "SELECT fingerprint, status, labels_json, received_at FROM alerts"
        " WHERE incident_id = ? ORDER BY id",
        (incident_id,),
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        labels = json.loads(row["labels_json"])
        entry = latest.setdefault(row["fingerprint"], {"first_seen": row["received_at"][:19]})
        entry.update(
            alertname=labels.get("alertname"),
            service=labels.get("service"),
            severity=labels.get("severity"),
            status=row["status"],
        )
        if labels.get("node"):
            entry["node"] = labels["node"]
    return list(latest.values())


def investigate_incident(
    incident: Incident,
    manager: IncidentManager,
    investigator: Investigator,
    conn: sqlite3.Connection,
    pipeline: Pipeline | None = None,
) -> Investigation:
    """Run one investigation and move the incident to its next state. With a
    pipeline, a diagnosed incident goes on to planning, policy and dry run."""
    manager.transition(incident.id, IncidentState.INVESTIGATING, reason="investigation started")
    run = investigator.investigate(incident, incident_alerts(conn, incident.id))
    categories = {cause.category for cause in run.diagnosis.root_causes}
    if RootCauseCategory.UNKNOWN in categories:
        manager.transition(
            incident.id, IncidentState.ESCALATED, reason=f"no diagnosis: {run.stopped_because}"
        )
    else:
        manager.transition(incident.id, IncidentState.DIAGNOSED, reason="diagnosis validated")
        if pipeline is not None:
            pipeline.remediate(run.diagnosis)
    return run


class Worker:
    """Investigates open incidents one at a time.

    An incident is left alone for `settle_seconds` after it opens, so alerts
    that belong together have time to arrive before the investigation starts.
    """

    def __init__(
        self,
        manager: IncidentManager,
        investigator: Investigator,
        conn: sqlite3.Connection,
        *,
        settle_seconds: float = 45,
        poll_seconds: float = 5,
        pipeline: Pipeline | None = None,
    ) -> None:
        self._manager = manager
        self._investigator = investigator
        self._conn = conn
        self._pipeline = pipeline
        self._settle = timedelta(seconds=settle_seconds)
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="investigator", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> Investigation | None:
        """Investigate the oldest incident that is ready, if any."""
        now = datetime.now(UTC)
        for incident in self._manager.all(active_only=True):
            if incident.state is IncidentState.OPEN and now - incident.opened_at >= self._settle:
                # Re-read: more alerts may have joined while it settled.
                return investigate_incident(
                    self._manager.get(incident.id),
                    self._manager,
                    self._investigator,
                    self._conn,
                    self._pipeline,
                )
        return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._pipeline is not None:
                    self._pipeline.expire_approvals()
                if self.run_once() is None:
                    self._stop.wait(self._poll)
            except Exception:
                # One failed investigation must not stop the worker.
                log.exception("investigation failed")
                self._stop.wait(self._poll)
