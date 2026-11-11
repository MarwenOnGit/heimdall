import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from heimdall.agent.investigator import Investigator
from heimdall.agent.worker import Worker, incident_alerts
from heimdall.core.audit import AuditLog
from heimdall.core.incidents import IncidentManager
from heimdall.core.models import Alert, IncidentState
from heimdall.core.store import connect
from heimdall.llm.base import Response
from heimdall.tools.dependencies import DependencyGraph
from heimdall.tools.registry import Tool, ToolRegistry

GRAPH = DependencyGraph({"frontend": ["cartservice"], "cartservice": []})


class Args(BaseModel):
    pass


class OneAnswer:
    model = "scripted"

    def __init__(self, reply: dict) -> None:
        self.reply = reply

    def complete(self, messages, response_schema=None) -> Response:
        return Response(json.dumps(self.reply), self.model, 10, 5, 0.1)


def diagnosis(category: str) -> dict:
    evidence = [
        {"tool_call_id": f"tc_{i}", "quote": "ready=0/1", "observation": "x"} for i in (1, 2)
    ]
    return {"hypotheses": [], "tool": None, "diagnosis": {"root_causes": [{
        "category": category, "summary": "s", "confidence": 0.9 if category != "unknown" else 0,
        "affected_resource": {"kind": "Deployment", "namespace": "boutique", "name": "frontend"},
        "evidence": [] if category == "unknown" else evidence}]}}  # fmt: skip


def setup(tmp_path: Path, reply: dict, opened: datetime):
    conn = connect(tmp_path / "db.sqlite")
    audit = AuditLog(conn)
    manager = IncidentManager(conn, audit, GRAPH, clock=lambda: opened)
    registry = ToolRegistry(audit)
    registry.register(Tool("pods", "list", Args, lambda a: "pod=frontend-x ready=0/1"))
    look = lambda incident: [("pods", {}), ("pods", {})]  # noqa: E731
    investigator = Investigator(OneAnswer(reply), registry, audit, first_look=look)
    worker = Worker(manager, investigator, conn, settle_seconds=45)
    alert = Alert.model_validate(
        {"status": "firing", "startsAt": "2026-11-10T12:00:00Z", "fingerprint": "f1",
         "labels": {"alertname": "HighErrorRate", "namespace": "boutique", "service": "frontend",
                    "severity": "critical"}}
    )  # fmt: skip
    incident = manager.ingest(alert, confirmed=True)
    assert incident is not None
    return manager, worker, conn, incident


def test_incident_waits_for_related_alerts_before_investigation(tmp_path: Path) -> None:
    manager, worker, _, incident = setup(tmp_path, diagnosis("bad_release"), datetime.now(UTC))

    assert worker.run_once() is None
    assert manager.get(incident.id).state is IncidentState.OPEN


def test_settled_incident_is_investigated_and_marked_diagnosed(tmp_path: Path) -> None:
    opened = datetime.now(UTC) - timedelta(minutes=2)
    manager, worker, _, incident = setup(tmp_path, diagnosis("bad_release"), opened)

    run = worker.run_once()

    assert run is not None and run.stopped_because == "diagnosed"
    assert manager.get(incident.id).state is IncidentState.DIAGNOSED
    assert worker.run_once() is None  # nothing left to do


def test_unknown_diagnosis_escalates(tmp_path: Path) -> None:
    opened = datetime.now(UTC) - timedelta(minutes=2)
    manager, worker, _, incident = setup(tmp_path, diagnosis("unknown"), opened)

    worker.run_once()

    assert manager.get(incident.id).state is IncidentState.ESCALATED


def test_incident_alerts_are_deduplicated_with_latest_status(tmp_path: Path) -> None:
    manager, _, conn, incident = setup(tmp_path, diagnosis("unknown"), datetime.now(UTC))
    resolved = Alert.model_validate(
        {"status": "resolved", "startsAt": "2026-11-10T12:00:00Z", "fingerprint": "f1",
         "labels": {"alertname": "HighErrorRate", "namespace": "boutique", "service": "frontend"}}
    )  # fmt: skip
    manager.ingest(resolved, confirmed=True)

    (alert,) = incident_alerts(conn, incident.id)

    assert (alert["alertname"], alert["service"], alert["status"]) == (
        "HighErrorRate", "frontend", "resolved")  # fmt: skip


def test_diagnosed_incident_is_handed_to_the_pipeline(tmp_path: Path) -> None:
    opened = datetime.now(UTC) - timedelta(minutes=2)
    manager, worker, _, incident = setup(tmp_path, diagnosis("bad_release"), opened)
    seen = []

    class RecordingPipeline:
        def remediate(self, diagnosis):
            seen.append(diagnosis.incident_id)

    worker._pipeline = RecordingPipeline()  # noqa: SLF001

    worker.run_once()

    assert seen == [incident.id]


def test_unknown_diagnosis_never_reaches_the_pipeline(tmp_path: Path) -> None:
    opened = datetime.now(UTC) - timedelta(minutes=2)
    _, worker, _, _ = setup(tmp_path, diagnosis("unknown"), opened)

    class Forbidden:
        def remediate(self, diagnosis):
            raise AssertionError("an unknown cause must not be planned for")

    worker._pipeline = Forbidden()  # noqa: SLF001

    worker.run_once()
