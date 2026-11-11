import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from heimdall.agent.pipeline import Pipeline
from heimdall.agent.planner import Planner
from heimdall.core.actions import ActionStatus, ActionStore
from heimdall.core.audit import AuditLog
from heimdall.core.incidents import IncidentManager
from heimdall.core.models import ActionProposal, Alert, Diagnosis, IncidentState
from heimdall.core.store import connect
from heimdall.executor.executor import Executor
from heimdall.llm.base import Response
from heimdall.policy.gate import OpaEvalGate
from heimdall.policy.input_builder import InputBuilder
from heimdall.tools.dependencies import DependencyGraph
from tests.unit.fake_kube import IMAGE_V1, FakeKube

OPA = Path(__file__).parents[2] / ".bin" / "opa"
pytestmark = pytest.mark.skipif(not OPA.exists(), reason="run infra/tools.sh to download opa")

GRAPH = DependencyGraph({"frontend": ["cartservice"], "cartservice": ["redis-cart"],
                         "redis-cart": []})  # fmt: skip


class ScriptedLLM:
    model = "scripted"

    def __init__(self, proposals: list[dict]) -> None:
        self.proposals = proposals
        self.calls = 0

    def complete(self, messages, response_schema=None) -> Response:
        self.calls += 1
        return Response(json.dumps({"proposals": self.proposals}), self.model, 50, 10, 0.1)


def proposal(kind: str, name: str | None = "frontend", **params) -> dict:
    out = {"type": kind, "justification": "because", "expected_effect": "healthy", **params}
    if name:
        out.update(namespace="boutique", name=name)
    return out


class World:
    """Everything a pipeline run needs, with the real OPA policy and a fake
    Kubernetes API."""

    def __init__(self, tmp_path: Path, proposals: list[dict]) -> None:
        conn = connect(tmp_path / "db.sqlite")
        self.audit = AuditLog(conn)
        self.actions = ActionStore(conn)
        self.manager = IncidentManager(conn, self.audit, GRAPH)
        self.kube = FakeKube()
        self.kube.add("frontend")
        self.kube.add("redis-cart", stateful=True, replicas=0)
        self.llm = ScriptedLLM(proposals)
        # Revision 1 was created long before revision 2, so it counts as proven.
        self.pipeline = Pipeline(
            Planner(self.llm, self.audit),
            InputBuilder(self.kube.client(), self.actions, mode="dry_run"),
            OpaEvalGate(OPA),
            Executor(self.kube.client(), self.kube.client(), self.actions, self.audit),
            self.manager,
            self.audit,
        )

    def diagnosed(self, category: str, name: str, confidence: float = 0.9) -> Diagnosis:
        alert = Alert.model_validate(
            {"status": "firing", "startsAt": datetime.now(UTC).isoformat(), "fingerprint": name,
             "labels": {"alertname": "HighErrorRate", "namespace": "boutique", "service": name}}
        )  # fmt: skip
        incident = self.manager.ingest(alert, confirmed=True)
        assert incident is not None
        self.manager.transition(incident.id, IncidentState.INVESTIGATING, reason="t")
        self.manager.transition(incident.id, IncidentState.DIAGNOSED, reason="t")
        resource = {"kind": "Deployment", "namespace": "boutique", "name": name}
        cause = {"category": category, "summary": f"{category} on {name}",
                 "confidence": confidence, "affected_resource": resource}  # fmt: skip
        return Diagnosis.model_validate({"incident_id": incident.id, "root_causes": [cause]})

    def state(self, diagnosis: Diagnosis) -> IncidentState:
        return self.manager.get(diagnosis.incident_id).state


def statuses(result) -> list[tuple[str, str, str]]:
    return [(p.type, d.decision, o.status) for p, d, o in result.steps]


def test_bad_release_is_rolled_back_in_dry_run(tmp_path: Path) -> None:
    """Scenario s04, end to end except for the model and the cluster."""
    world = World(tmp_path, [proposal("rollback_deployment", to_revision=1),
                             proposal("open_pr", summary="revert the release commit")])  # fmt: skip
    diagnosis = world.diagnosed("bad_release", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [
        ("rollback_deployment", "allow", "dry_run"),
        ("open_pr", "allow", "recorded"),
    ]
    (patch,) = world.kube.patches
    assert patch["dry_run"] is True
    assert patch["body"]["metadata"] == {"resourceVersion": "100"}
    assert patch["body"]["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE_V1
    assert world.state(diagnosis) is IncidentState.ESCALATED
    assert "rollback_deployment frontend: dry_run" in result.summary


def test_redis_needs_a_human_before_it_is_scaled_back(tmp_path: Path) -> None:
    """Scenario s07: the right fix, on a stateful target, waits for approval."""
    world = World(tmp_path, [proposal("scale_deployment", name="redis-cart", replicas=1)])
    diagnosis = world.diagnosed("dependency_down", "redis-cart")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("scale_deployment", "require_approval", "awaiting_approval")]
    assert world.kube.patches == []
    assert world.state(diagnosis) is IncidentState.ESCALATED


def test_policy_denial_stops_the_rest_of_the_plan(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("rollback_deployment", to_revision=2),  # the bad one itself
                             proposal("open_pr", summary="x")])  # fmt: skip
    diagnosis = world.diagnosed("bad_release", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("rollback_deployment", "deny", "denied")]
    assert "never healthy" in result.steps[0][2].detail
    assert world.kube.patches == []


def test_admission_rejection_stops_the_plan_and_is_recorded(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("rollback_deployment", to_revision=1),
                             proposal("open_pr", summary="x")])  # fmt: skip
    world.kube.reject_with = (422, "ValidatingAdmissionPolicy denied: image not allowed")
    diagnosis = world.diagnosed("bad_release", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("rollback_deployment", "allow", "rejected_by_api")]
    assert world.state(diagnosis) is IncidentState.ESCALATED


def test_frozen_heimdall_touches_nothing(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("rollback_deployment", to_revision=1)])
    world.actions.set_frozen(True)
    diagnosis = world.diagnosed("bad_release", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("rollback_deployment", "deny", "denied")]
    assert "frozen" in result.steps[0][2].detail
    assert world.kube.patches == []


def test_nothing_wrong_resolves_the_incident_without_the_model(tmp_path: Path) -> None:
    world = World(tmp_path, [])
    diagnosis = world.diagnosed("no_action_needed", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("none", "allow", "recorded")]
    assert world.state(diagnosis) is IncidentState.RESOLVED
    assert world.llm.calls == 0


def test_low_confidence_escalates_and_touches_nothing(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("rollback_deployment", to_revision=1)])
    diagnosis = world.diagnosed("bad_release", "frontend", confidence=0.5)

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("escalate", "allow", "recorded")]
    assert world.kube.patches == [] and world.llm.calls == 0


def test_fourth_write_in_an_incident_is_denied(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("restart_deployment")])
    diagnosis = world.diagnosed("dependency_down", "frontend")
    for replicas in (2, 3, 4):
        world.actions.record(
            diagnosis.incident_id,
            ActionProposal.model_validate(proposal("scale_deployment", replicas=replicas)),
            decision="allow", status=ActionStatus.DRY_RUN, detail="ok",
        )  # fmt: skip

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("restart_deployment", "deny", "denied")]
    assert "already 3 write actions" in result.steps[0][2].detail


def test_the_whole_run_is_in_the_audit_log_in_order(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("rollback_deployment", to_revision=1)])
    diagnosis = world.diagnosed("bad_release", "frontend")

    world.pipeline.remediate(diagnosis)

    kinds = [e.event_type for e in world.audit.events(diagnosis.incident_id)]
    assert kinds[-6:] == ["state_transition", "llm_call", "proposal", "policy_decision",
                          "action_executed", "state_transition"]  # fmt: skip
    assert world.audit.verify_chain() is None
