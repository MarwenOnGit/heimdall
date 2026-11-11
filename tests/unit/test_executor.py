from datetime import UTC, datetime
from pathlib import Path

import pytest

from heimdall.core.actions import ActionStatus, ActionStore
from heimdall.core.audit import AuditLog
from heimdall.core.models import ActionProposal, Diagnosis
from heimdall.core.store import connect
from heimdall.executor.executor import Executor
from heimdall.policy.gate import Decision
from heimdall.policy.input_builder import InputBuilder, TargetMissing
from tests.unit.fake_kube import IMAGE_V1, FakeKube

NOW = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
ALLOW = Decision(decision="allow")


def proposal(kind: str, name: str | None = "frontend", **params) -> ActionProposal:
    target = {"namespace": "boutique", "name": name} if name else {}
    return ActionProposal(type=kind, justification="because", expected_effect="healthy",
                          **target, **params)  # fmt: skip


def diagnosis(name: str = "frontend", confidence: float = 0.9) -> Diagnosis:
    resource = {"kind": "Deployment", "namespace": "boutique", "name": name}
    cause = {"category": "bad_release", "summary": "s", "confidence": confidence,
             "affected_resource": resource}  # fmt: skip
    return Diagnosis.model_validate({"incident_id": "inc_1", "root_causes": [cause]})


@pytest.fixture
def kube() -> FakeKube:
    fake = FakeKube()
    fake.add("frontend")
    fake.add("redis-cart", stateful=True, replicas=0)
    return fake


@pytest.fixture
def actions(tmp_path: Path) -> ActionStore:
    return ActionStore(connect(tmp_path / "db.sqlite"), clock=lambda: NOW)


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(connect(tmp_path / "db.sqlite"))


@pytest.fixture
def executor(kube: FakeKube, actions: ActionStore, audit: AuditLog) -> Executor:
    return Executor(kube.client(), kube.client(), actions, audit, dry_run=True, clock=lambda: NOW)


# ------------------------------------------------------------ input builder


def test_input_is_built_from_the_cluster_and_the_action_record(
    kube: FakeKube, actions: ActionStore
) -> None:
    earlier = proposal("restart_deployment")
    actions.record("inc_0", earlier, decision="allow", status=ActionStatus.DRY_RUN, detail="ok")
    builder = InputBuilder(kube.client(), actions, mode="dry_run", clock=lambda: NOW)

    built = builder.build(proposal("scale_deployment", replicas=3), diagnosis())

    assert built.resource_version == "100"
    assert built.data["action"] == {"type": "scale_deployment", "namespace": "boutique",
                                    "name": "frontend", "replicas": 3}  # fmt: skip
    assert built.data["current"] == {"replicas": 1, "stateful": False, "resource_version": "100",
                                     "limits": {"memory": "128Mi", "cpu": "200m"}}  # fmt: skip
    assert built.data["incident"]["confidence"] == 0.9
    assert built.data["incident"]["affected_resources"] == [
        {"kind": "Deployment", "namespace": "boutique", "name": "frontend"}
    ]
    assert built.data["incident"]["write_actions_so_far"] == 0
    assert [r["incident_id"] for r in built.data["recent_actions"]] == ["inc_0"]
    assert (built.data["mode"], built.data["frozen"]) == ("dry_run", False)


def test_revision_history_credits_superseded_revisions_only(
    kube: FakeKube, actions: ActionStore
) -> None:
    builder = InputBuilder(kube.client(), actions, mode="dry_run", clock=lambda: NOW)

    built = builder.build(proposal("rollback_deployment", to_revision=1), diagnosis())

    # Revision 1 served from 09:00 on the 9th until revision 2 replaced it at
    # 13:02 on the 10th. Revision 2 is current and not a rollback target.
    assert built.data["revision_history"] == [{"revision": 1, "available_minutes": 1682}]


def test_stateful_label_and_freeze_flag_are_reported(kube: FakeKube, actions: ActionStore) -> None:
    actions.set_frozen(True)
    builder = InputBuilder(kube.client(), actions, mode="dry_run")

    built = builder.build(
        proposal("scale_deployment", name="redis-cart", replicas=1), diagnosis("redis-cart")
    )

    assert built.data["current"]["stateful"] is True
    assert built.data["current"]["replicas"] == 0
    assert built.data["frozen"] is True


def test_passive_proposals_need_no_cluster_state(kube: FakeKube, actions: ActionStore) -> None:
    built = InputBuilder(kube.client(), actions, mode="dry_run").build(
        proposal("escalate", name=None, summary="help"), diagnosis()
    )
    assert built.resource_version is None and built.data["current"] == {}


@pytest.mark.parametrize(("namespace", "name"), [("boutique", "ghost"), ("kube-system", "coredns")])
def test_missing_or_unreachable_target_is_an_error(
    kube: FakeKube, actions: ActionStore, namespace: str, name: str
) -> None:
    builder = InputBuilder(kube.client(), actions, mode="dry_run")
    bad = ActionProposal(type="restart_deployment", namespace=namespace, name=name,
                         justification="j", expected_effect="e")  # fmt: skip

    with pytest.raises(TargetMissing):
        builder.build(bad, diagnosis())


# ----------------------------------------------------------------- executor


def test_restart_is_sent_as_a_conditional_dry_run(executor: Executor, kube: FakeKube) -> None:
    outcome = executor.execute("inc_1", proposal("restart_deployment"), ALLOW, "100")

    assert outcome.status is ActionStatus.DRY_RUN
    (patch,) = kube.patches
    assert patch["dry_run"] is True
    assert patch["path"].endswith("/deployments/frontend")
    assert patch["content_type"] == "application/merge-patch+json"
    assert patch["body"] == {
        "metadata": {"resourceVersion": "100"},
        "spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": "2026-11-10T13:30:00+00:00"}}}},
    }  # fmt: skip


def test_scale_uses_the_scale_subresource(executor: Executor, kube: FakeKube) -> None:
    executor.execute("inc_1", proposal("scale_deployment", replicas=3), ALLOW, "100")

    (patch,) = kube.patches
    assert patch["path"].endswith("/deployments/frontend/scale")
    assert patch["body"]["spec"] == {"replicas": 3}


def test_patch_resources_touches_only_the_limits(executor: Executor, kube: FakeKube) -> None:
    change = proposal("patch_resources", container="server", limits={"memory": "256Mi"})

    executor.execute("inc_1", change, ALLOW, "100")

    (patch,) = kube.patches
    assert patch["content_type"] == "application/strategic-merge-patch+json"
    assert patch["body"]["spec"]["template"]["spec"]["containers"] == [
        {"name": "server", "resources": {"limits": {"memory": "256Mi"}}}
    ]


def test_rollback_restores_the_image_of_the_target_revision_and_nothing_else(
    executor: Executor, kube: FakeKube
) -> None:
    outcome = executor.execute(
        "inc_1", proposal("rollback_deployment", to_revision=1), ALLOW, "100"
    )

    assert outcome.status is ActionStatus.DRY_RUN
    (patch,) = kube.patches
    assert patch["body"]["spec"]["template"]["spec"]["containers"] == [
        {"name": "server", "image": IMAGE_V1}
    ]


def test_rollback_to_an_unknown_revision_fails_without_writing(
    executor: Executor, kube: FakeKube
) -> None:
    outcome = executor.execute(
        "inc_1", proposal("rollback_deployment", to_revision=9), ALLOW, "100"
    )

    assert outcome.status is ActionStatus.FAILED
    assert kube.patches == []


def test_change_since_the_policy_check_is_a_conflict(executor: Executor, kube: FakeKube) -> None:
    """No time-of-check to time-of-use gap: the write names the version the
    policy saw, and the API server refuses if it moved on."""
    outcome = executor.execute("inc_1", proposal("restart_deployment"), ALLOW, "99")

    assert outcome.status is ActionStatus.CONFLICT


def test_admission_rejection_is_reported_as_such(executor: Executor, kube: FakeKube) -> None:
    kube.reject_with = (422, "ValidatingAdmissionPolicy 'heimdall-actor-deployments' denied")

    outcome = executor.execute("inc_1", proposal("restart_deployment"), ALLOW, "100")

    assert outcome.status is ActionStatus.REJECTED_BY_API
    assert "ValidatingAdmissionPolicy" in outcome.detail


@pytest.mark.parametrize(
    ("decision", "status"),
    [
        (Decision(decision="deny", reasons=["replicas must be between 1 and 5"]),
         ActionStatus.DENIED),
        (Decision(decision="require_approval", reasons=["target is stateful"]),
         ActionStatus.AWAITING_APPROVAL),
    ],
)  # fmt: skip
def test_denied_and_unapproved_actions_never_reach_the_cluster(
    executor: Executor, kube: FakeKube, decision: Decision, status: ActionStatus
) -> None:
    outcome = executor.execute("inc_1", proposal("scale_deployment", replicas=3), decision, "100")

    assert outcome.status is status
    assert outcome.detail == decision.reasons[0]
    assert kube.patches == []


def test_escalation_and_pull_requests_are_recorded_not_run(
    executor: Executor, kube: FakeKube, actions: ActionStore
) -> None:
    executor.execute("inc_1", proposal("escalate", name=None, summary="needs a human"), ALLOW, None)
    pr = executor.execute("inc_1", proposal("open_pr", summary="revert the commit"), ALLOW, None)

    assert kube.patches == []
    assert pr.status is ActionStatus.RECORDED and "no git server configured" in pr.detail
    assert [a.status for a in actions.for_incident("inc_1")] == [ActionStatus.RECORDED] * 2


def test_the_same_action_is_never_applied_twice(executor: Executor, kube: FakeKube) -> None:
    """After a restart mid-incident, replaying the plan must not repeat it."""
    first = executor.execute("inc_1", proposal("restart_deployment"), ALLOW, "100")
    second = executor.execute("inc_1", proposal("restart_deployment"), ALLOW, "100")

    assert first == second
    assert len(kube.patches) == 1


def test_a_failed_action_may_be_retried(executor: Executor, kube: FakeKube) -> None:
    executor.execute("inc_1", proposal("restart_deployment"), ALLOW, "99")  # conflict
    retry = executor.execute("inc_1", proposal("restart_deployment"), ALLOW, "100")

    assert retry.status is ActionStatus.DRY_RUN
    assert len(kube.patches) == 2


def test_live_mode_sends_a_real_write(
    kube: FakeKube, actions: ActionStore, audit: AuditLog
) -> None:
    live = Executor(kube.client(), kube.client(), actions, audit, dry_run=False)

    outcome = live.execute("inc_1", proposal("restart_deployment"), ALLOW, "100")

    assert outcome.status is ActionStatus.EXECUTED
    assert kube.patches[0]["dry_run"] is False


def test_every_outcome_is_audited(executor: Executor, audit: AuditLog) -> None:
    executor.execute("inc_1", proposal("restart_deployment"), ALLOW, "100")
    executor.execute("inc_1", proposal("scale_deployment", replicas=9),
                     Decision(decision="deny", reasons=["too many"]), "100")  # fmt: skip

    events = [e.payload for e in audit.events("inc_1") if e.event_type == "action_executed"]
    assert [(e["status"], e["decision"], e["dry_run"]) for e in events] == [
        ("dry_run", "allow", True), ("denied", "deny", True)]  # fmt: skip
    assert audit.verify_chain() is None


# --------------------------------------------------------------------- undo


@pytest.fixture
def live(kube: FakeKube, actions: ActionStore, audit: AuditLog) -> Executor:
    return Executor(kube.client(), kube.client(), actions, audit, dry_run=False, clock=lambda: NOW)


def test_previous_state_is_recorded_before_a_write(live: Executor, actions: ActionStore) -> None:
    change = proposal("scale_deployment", replicas=3)

    live.execute("inc_1", change, ALLOW, "100")

    record = actions.find("inc_1", change)
    assert record is not None
    assert record.previous == {
        "replicas": 1,
        "containers": [{"name": "server", "image": "repo/frontend:v0.11.1",
                        "limits": {"memory": "128Mi", "cpu": "200m"}}],
    }  # fmt: skip


@pytest.mark.parametrize(
    ("change", "restored"),
    [
        ({"kind": "scale_deployment", "replicas": 3}, {"spec": {"replicas": 1}}),
        (
            {"kind": "patch_resources", "container": "server", "limits": {"memory": "256Mi"}},
            {"spec": {"template": {"spec": {"containers": [
                {"name": "server", "resources": {"limits": {"memory": "128Mi", "cpu": "200m"}}}
            ]}}}},
        ),
        (
            {"kind": "rollback_deployment", "to_revision": 1},
            {"spec": {"template": {"spec": {"containers": [
                {"name": "server", "image": "repo/frontend:v0.11.1"}]}}}},
        ),
    ],
)  # fmt: skip
def test_undo_restores_what_the_action_changed(
    live: Executor, kube: FakeKube, actions: ActionStore, change: dict, restored: dict
) -> None:
    action = proposal(**change)
    live.execute("inc_1", action, ALLOW, "100")

    outcome = live.undo("inc_1", action)

    assert outcome.status is ActionStatus.UNDONE
    assert kube.patches[-1]["body"] == restored
    assert kube.patches[-1]["dry_run"] is False
    found = actions.find("inc_1", action)
    assert found is not None and found.status is ActionStatus.UNDONE


def test_undone_actions_no_longer_count_as_writes(live: Executor, actions: ActionStore) -> None:
    action = proposal("scale_deployment", replicas=3)
    live.execute("inc_1", action, ALLOW, "100")
    assert actions.writes_so_far("inc_1") == 1

    live.undo("inc_1", action)

    assert actions.writes_so_far("inc_1") == 0


def test_undo_refused_by_the_api_server_is_reported(live: Executor, kube: FakeKube) -> None:
    """For example scaling Redis back to 0, which the admission policy forbids."""
    action = proposal("scale_deployment", name="redis-cart", replicas=1)
    live.execute("inc_1", action, ALLOW, "100")
    kube.reject_with = (422, "ValidatingAdmissionPolicy denied: replicas must be between 1 and 5")

    outcome = live.undo("inc_1", action)

    assert outcome.status is ActionStatus.FAILED
    assert "undo refused" in outcome.detail


def test_restart_and_unexecuted_actions_have_nothing_to_undo(
    live: Executor, kube: FakeKube, audit: AuditLog
) -> None:
    restart = proposal("restart_deployment")
    live.execute("inc_1", restart, ALLOW, "100")
    writes = len(kube.patches)

    assert live.undo("inc_1", restart).status is ActionStatus.UNDONE
    assert live.undo("inc_1", proposal("scale_deployment", replicas=4)).status is (
        ActionStatus.FAILED
    )
    assert len(kube.patches) == writes
    # Only the attempt on an action that really ran is an audit event.
    assert [e.event_type for e in audit.events("inc_1")].count("rollback") == 1
