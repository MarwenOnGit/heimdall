import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from heimdall.agent.pipeline import Pipeline
from heimdall.agent.planner import Planner
from heimdall.core.actions import ActionStatus, ActionStore
from heimdall.core.approvals import ApprovalStatus, ApprovalStore
from heimdall.core.audit import AuditLog, EventType
from heimdall.core.incidents import IncidentManager
from heimdall.core.models import ActionProposal, Alert, Diagnosis, IncidentState
from heimdall.core.store import connect
from heimdall.executor.executor import Executor
from heimdall.executor.gitops import GitOps
from heimdall.executor.verifier import Verification
from heimdall.llm.base import Response
from heimdall.policy.gate import OpaEvalGate
from heimdall.policy.input_builder import InputBuilder
from heimdall.tools.dependencies import DependencyGraph
from tests.unit.fake_gitea import FakeGitea
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


class FakeVerifier:
    """Answers each verification from a script: True means recovered."""

    def __init__(self, recovers: list[bool]) -> None:
        self.recovers = list(recovers)
        self.checked: list[list[str]] = []

    def verify(self, checks) -> Verification:
        self.checked.append([c.description for c in checks])
        ok = self.recovers.pop(0)
        return Verification(ok, 45.0, {c.description: ok for c in checks})


class World:
    """Everything a pipeline run needs, with the real OPA policy and a fake
    Kubernetes API."""

    def __init__(
        self,
        tmp_path: Path,
        proposals: list[dict],
        *,
        live: bool = False,
        recovers: list[bool] | None = None,
        approvals: bool = False,
        gitea: FakeGitea | None = None,
    ) -> None:
        conn = connect(tmp_path / "db.sqlite")
        self.audit = AuditLog(conn)
        self.actions = ActionStore(conn)
        self.manager = IncidentManager(conn, self.audit, GRAPH)
        self.kube = FakeKube()
        self.kube.add("frontend")
        self.kube.add("redis-cart", stateful=True, replicas=0)
        self.llm = ScriptedLLM(proposals)
        # Revision 1 was created long before revision 2, so it counts as proven.
        self.kube.applications = True
        self.gitops = (
            GitOps(gitea.client(), "heimdall/gitops", self.kube.client()) if gitea else None
        )
        self.verifier = FakeVerifier(recovers or [])
        self.sent: list[tuple[str, str]] = []
        self.approvals = (
            ApprovalStore(conn, b"0123456789abcdef0123456789abcdef") if approvals else None
        )
        notifier = type(
            "Recorder", (), {"send": lambda _, title, text: self.sent.append((title, text))}
        )()
        self.pipeline = Pipeline(
            Planner(self.llm, self.audit),
            InputBuilder(self.kube.client(), self.actions, mode="live" if live else "dry_run",
                         gitops=self.gitops),
            OpaEvalGate(OPA),
            Executor(self.kube.client(), self.kube.client(), self.actions, self.audit,
                     dry_run=not live, gitops=self.gitops),
            self.manager,
            self.audit,
            live=live,
            verifier=self.verifier,
            approvals=self.approvals,
            notifier=notifier,
            gitops=self.gitops,
            public_url="http://heimdall.test",
        )  # fmt: skip

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
        diagnosis = Diagnosis.model_validate({"incident_id": incident.id, "root_causes": [cause]})
        # The investigator records its diagnosis; an approval is resumed from it.
        self.audit.append(EventType.DIAGNOSIS, {"diagnosis": diagnosis.model_dump(mode="json")},
                          actor="investigator", incident_id=incident.id)  # fmt: skip
        return diagnosis

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
    assert "rollback_deployment boutique/frontend to revision 1: dry_run" in result.summary


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


# --------------------------------------------------------------- live mode


def states(world: World, diagnosis: Diagnosis) -> list[str]:
    return [
        e.payload["to"]
        for e in world.audit.events(diagnosis.incident_id)
        if e.event_type == "state_transition"
    ]


def test_live_rollback_is_verified_and_the_incident_is_mitigated(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("rollback_deployment", to_revision=1),
                             proposal("open_pr", summary="revert the release commit")],
                  live=True, recovers=[True])  # fmt: skip
    diagnosis = world.diagnosed("bad_release", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [
        ("rollback_deployment", "allow", "executed"),
        ("open_pr", "allow", "recorded"),
    ]
    assert world.kube.patches[0]["dry_run"] is False
    assert "frontend success rate is at least 99%" in world.verifier.checked[0]
    # Healthy again, but git still has the bad release until the PR is merged.
    assert world.state(diagnosis) is IncidentState.MITIGATED
    assert states(world, diagnosis)[-4:] == ["planning", "executing", "verifying", "mitigated"]
    assert world.sent[-1][0].startswith("Mitigated:")


def test_live_fix_that_does_not_work_is_undone_and_escalated(tmp_path: Path) -> None:
    """Scenario s15: the action is allowed and applied, but nothing recovers."""
    world = World(tmp_path, [proposal("scale_deployment", replicas=3)], live=True,
                  recovers=[False])  # fmt: skip
    diagnosis = world.diagnosed("resource_saturation", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("scale_deployment", "allow", "undone")]
    assert result.rolled_back
    scaled, restored = world.kube.patches
    assert scaled["body"]["spec"] == {"replicas": 3}
    assert restored["body"]["spec"] == {"replicas": 1}
    assert states(world, diagnosis)[-4:] == ["executing", "verifying", "rolled_back", "escalated"]
    assert "did not recover" in result.summary and "undone" in result.summary
    kinds = [e.event_type for e in world.audit.events(diagnosis.incident_id)]
    assert "verification" in kinds and "rollback" in kinds
    assert world.sent[-1][0].startswith("Escalated:")


def test_live_plan_with_two_writes_verifies_each(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("scale_deployment", replicas=2),
                             proposal("restart_deployment")],
                  live=True, recovers=[True, True])  # fmt: skip
    diagnosis = world.diagnosed("dependency_down", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert [o.status for _, _, o in result.steps] == ["executed", "executed"]
    assert len(world.verifier.checked) == 2
    assert world.state(diagnosis) is IncidentState.RESOLVED
    assert states(world, diagnosis)[-7:] == [
        "planning", "executing", "verifying", "planning", "executing", "verifying", "resolved"
    ]  # fmt: skip


def test_live_mode_without_a_verifier_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path, [])
    with pytest.raises(ValueError, match="needs a verifier"):
        Pipeline(
            world.pipeline._planner,
            world.pipeline._builder,
            world.pipeline._gate,  # noqa: SLF001
            world.pipeline._executor,
            world.manager,
            world.audit,
            live=True,
        )  # noqa: SLF001


# --------------------------------------------------------------- approvals


def redis_world(tmp_path: Path, **kwargs) -> tuple[World, Diagnosis]:
    world = World(tmp_path, [proposal("scale_deployment", name="redis-cart", replicas=1)],
                  approvals=True, **kwargs)  # fmt: skip
    return world, world.diagnosed("dependency_down", "redis-cart")


def test_stateful_target_waits_for_a_human_and_says_exactly_what_for(tmp_path: Path) -> None:
    world, diagnosis = redis_world(tmp_path, live=True)

    result = world.pipeline.remediate(diagnosis)

    assert result.awaiting is not None
    assert world.state(diagnosis) is IncidentState.AWAITING_APPROVAL
    assert world.kube.patches == []
    title, text = world.sent[-1]
    assert title.startswith("Approval needed:")
    assert "scale_deployment boutique/redis-cart to 1 replicas" in text
    assert "target is stateful" in text
    assert f"http://heimdall.test/approvals/{result.awaiting.id}" in text


def test_approved_action_runs_after_the_policy_is_checked_again(tmp_path: Path) -> None:
    world, diagnosis = redis_world(tmp_path, live=True, recovers=[True])
    pending = world.pipeline.remediate(diagnosis).awaiting
    assert pending is not None and world.approvals is not None
    approved = world.approvals.decide(pending.id, world.approvals.token(pending), approve=True)

    result = world.pipeline.resume(approved, approved=True)

    assert statuses(result) == [("scale_deployment", "allow", "executed")]
    assert result.steps[0][1].reasons == ["approved by a human: target is stateful"]
    assert world.kube.patches[0]["body"]["spec"] == {"replicas": 1}
    assert world.state(diagnosis) is IncidentState.RESOLVED
    decisions = [e.payload["decision"] for e in world.audit.events(diagnosis.incident_id)
                 if e.event_type == "policy_decision"]  # fmt: skip
    assert decisions == ["require_approval", "allow"]


def test_rejection_escalates_and_touches_nothing(tmp_path: Path) -> None:
    world, diagnosis = redis_world(tmp_path, live=True)
    pending = world.pipeline.remediate(diagnosis).awaiting
    assert pending is not None and world.approvals is not None
    rejected = world.approvals.decide(pending.id, world.approvals.token(pending), approve=False)

    result = world.pipeline.resume(rejected, approved=False)

    assert result.final_state is IncidentState.ESCALATED
    assert "a human rejected" in result.summary
    assert world.kube.patches == []


def test_approval_is_void_if_the_target_changed_meanwhile(tmp_path: Path) -> None:
    world, diagnosis = redis_world(tmp_path, live=True)
    pending = world.pipeline.remediate(diagnosis).awaiting
    assert pending is not None and world.approvals is not None
    approved = world.approvals.decide(pending.id, world.approvals.token(pending), approve=True)
    world.kube.deployments["redis-cart"]["metadata"]["resourceVersion"] = "101"

    result = world.pipeline.resume(approved, approved=True)

    assert statuses(result) == [("scale_deployment", "deny", "denied")]
    assert "approval is void" in result.steps[0][2].detail
    assert world.kube.patches == []
    current = world.approvals.get(pending.id)
    assert current is not None and current.status is ApprovalStatus.VOID
    assert world.state(diagnosis) is IncidentState.ESCALATED


def test_approval_does_not_override_a_freeze(tmp_path: Path) -> None:
    world, diagnosis = redis_world(tmp_path, live=True)
    pending = world.pipeline.remediate(diagnosis).awaiting
    assert pending is not None and world.approvals is not None
    approved = world.approvals.decide(pending.id, world.approvals.token(pending), approve=True)
    world.actions.set_frozen(True)

    result = world.pipeline.resume(approved, approved=True)

    assert statuses(result) == [("scale_deployment", "deny", "denied")]
    assert world.kube.patches == []


def test_expired_approval_escalates(tmp_path: Path) -> None:
    world, diagnosis = redis_world(tmp_path, live=True)
    world.pipeline.remediate(diagnosis)
    assert world.approvals is not None
    world.approvals._clock = lambda: datetime.now(UTC).replace(year=2099)  # noqa: SLF001

    (result,) = world.pipeline.expire_approvals()

    assert result.final_state is IncidentState.ESCALATED
    assert "approval expired" in result.summary
    assert world.state(diagnosis) is IncidentState.ESCALATED
    assert world.pipeline.expire_approvals() == []


def test_in_dry_run_an_approved_action_is_still_only_a_dry_run(tmp_path: Path) -> None:
    world, diagnosis = redis_world(tmp_path)
    pending = world.pipeline.remediate(diagnosis).awaiting
    assert pending is not None and world.approvals is not None
    approved = world.approvals.decide(pending.id, world.approvals.token(pending), approve=True)

    result = world.pipeline.resume(approved, approved=True)

    assert statuses(result) == [("scale_deployment", "allow", "dry_run")]
    assert world.kube.patches[0]["dry_run"] is True
    assert world.state(diagnosis) is IncidentState.ESCALATED


# ------------------------------------------------------------------ gitops

OVERRIDE = "online-boutique/overrides/frontend-image.yaml"
KUSTOMIZATION = "online-boutique/kustomization.yaml"
APPLICATION = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/boutique"


def repo_with_bad_release() -> FakeGitea:
    gitea = FakeGitea({KUSTOMIZATION: "patches:\n- path: labels.yaml\n"})
    gitea.commit(
        "Release frontend v0.11.1",
        {OVERRIDE: "image: frontend:v0.11.1\n",
         KUSTOMIZATION: "patches:\n- path: labels.yaml\n- path: overrides/frontend-image.yaml\n"},
    )  # fmt: skip
    return gitea


def release_world(tmp_path: Path, gitea: FakeGitea, **kwargs) -> tuple[World, Diagnosis]:
    world = World(tmp_path, [proposal("rollback_deployment", to_revision=1),
                             proposal("open_pr", summary="Revision 2 fails requests.")],
                  gitea=gitea, **kwargs)  # fmt: skip
    return world, world.diagnosed("bad_release", "frontend")


def test_live_rollback_pauses_sync_then_opens_the_reconciling_pr(tmp_path: Path) -> None:
    """The whole of scenario s04: mitigate live, then bring git in line."""
    gitea = repo_with_bad_release()
    world, diagnosis = release_world(tmp_path, gitea, live=True, recovers=[True])

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [
        ("rollback_deployment", "allow", "executed"),
        ("open_pr", "allow", "pr_opened"),
    ]
    # Sync is paused before the write that creates drift, not after.
    paths = [p["path"] for p in world.kube.patches]
    assert paths == [APPLICATION, "/apis/apps/v1/namespaces/boutique/deployments/frontend"]
    assert world.kube.patches[0]["body"] == {"spec": {"syncPolicy": {"automated": None}}}
    assert (
        gitea.pulls[0]["title"]
        == 'Revert "Release frontend v0.11.1" (' + diagnosis.incident_id + ")"
    )
    assert OVERRIDE in gitea.files("main")  # nothing is merged by Heimdall
    assert world.state(diagnosis) is IncidentState.MITIGATED


def test_merged_pr_resumes_sync_and_resolves_the_incident(tmp_path: Path) -> None:
    gitea = repo_with_bad_release()
    world, diagnosis = release_world(tmp_path, gitea, live=True, recovers=[True])
    world.pipeline.remediate(diagnosis)
    assert world.pipeline.reconcile_merged(world.actions) == []  # not merged yet

    gitea.merge(1)
    closed = world.pipeline.reconcile_merged(world.actions)

    assert closed == [diagnosis.incident_id]
    assert world.state(diagnosis) is IncidentState.RESOLVED
    assert world.kube.patches[-1]["body"] == {
        "spec": {"syncPolicy": {"automated": {"prune": True, "selfHeal": False}}}
    }
    assert world.pipeline.reconcile_merged(world.actions) == []


def test_failed_live_rollback_is_undone_and_sync_is_resumed(tmp_path: Path) -> None:
    gitea = repo_with_bad_release()
    world, diagnosis = release_world(tmp_path, gitea, live=True, recovers=[False])

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("rollback_deployment", "allow", "undone")]
    bodies = [p["body"] for p in world.kube.patches if p["path"] == APPLICATION]
    assert bodies[0]["spec"]["syncPolicy"]["automated"] is None
    assert bodies[-1]["spec"]["syncPolicy"]["automated"] == {"prune": True, "selfHeal": False}
    assert gitea.pulls == []


def test_dry_run_describes_the_pull_request_without_opening_it(tmp_path: Path) -> None:
    gitea = repo_with_bad_release()
    world, diagnosis = release_world(tmp_path, gitea)

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result)[1] == ("open_pr", "allow", "recorded")
    assert "would open a pull request reverting" in result.steps[1][2].detail
    assert gitea.pulls == []
    assert all(p["path"] != APPLICATION for p in world.kube.patches)  # no pause in dry run


def test_pull_request_with_nothing_to_revert_is_denied(tmp_path: Path) -> None:
    gitea = FakeGitea({KUSTOMIZATION: "patches: []\n"})
    world = World(tmp_path, [proposal("open_pr", summary="x")], gitea=gitea, live=True)
    diagnosis = world.diagnosed("bad_config", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("open_pr", "deny", "denied")]
    assert "no change to frontend to revert" in result.steps[0][2].detail


def test_oversized_revert_is_denied_by_policy(tmp_path: Path) -> None:
    gitea = FakeGitea({KUSTOMIZATION: "patches: []\n"})
    big = "".join(f"line {i}\n" for i in range(80))
    listed = "patches:\n- path: overrides/frontend-image.yaml\n"
    gitea.commit("Rewrite frontend", {OVERRIDE: big, KUSTOMIZATION: listed})
    world = World(tmp_path, [proposal("open_pr", summary="x")], gitea=gitea, live=True)
    diagnosis = world.diagnosed("bad_config", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("open_pr", "deny", "denied")]
    assert "more than 50" in result.steps[0][2].detail
    assert gitea.pulls == []


def test_write_is_not_attempted_if_sync_cannot_be_paused(tmp_path: Path) -> None:
    gitea = repo_with_bad_release()
    world, diagnosis = release_world(tmp_path, gitea, live=True, recovers=[True])
    world.kube.applications = False  # the actor is not allowed to patch the Application

    result = world.pipeline.remediate(diagnosis)

    assert statuses(result) == [("rollback_deployment", "allow", "failed")]
    assert "could not pause GitOps sync" in result.steps[0][2].detail
    assert not any("deployments" in p["path"] for p in world.kube.patches)
