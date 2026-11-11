import json
from pathlib import Path

import pytest

from heimdall.agent.planner import ALLOWED, Plan, Planner, check, describe
from heimdall.core.audit import AuditLog
from heimdall.core.models import ActionProposal, ActionType, Diagnosis, RootCauseCategory
from heimdall.core.store import connect
from heimdall.llm.base import LLMError, Response


def diagnosis(category: str, name: str = "frontend", confidence: float = 0.9, **more) -> Diagnosis:
    causes = [
        {
            "category": category,
            "summary": f"{category} on {name}",
            "confidence": confidence,
            "affected_resource": {"kind": "Deployment", "namespace": "boutique", "name": name},
        }
    ]
    for extra_category, extra_name in more.get("also", []):
        causes.append(
            {
                "category": extra_category,
                "summary": "second cause",
                "confidence": 0.9,
                "affected_resource": {
                    "kind": "Deployment",
                    "namespace": "boutique",
                    "name": extra_name,
                },
            }
        )
    return Diagnosis.model_validate({"incident_id": "inc_1", "root_causes": causes})  # fmt: skip


def proposal(kind: str, name: str | None = "frontend", **params) -> dict:
    out = {"type": kind, "justification": "because", "expected_effect": "success rate > 99%",
           **params}  # fmt: skip
    if name:
        out.update(namespace="boutique", name=name)
    return out


class ScriptedLLM:
    model = "scripted"

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, messages, response_schema=None) -> Response:
        self.prompts.append(messages[0].content)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps({"proposals": reply})
        return Response(content, self.model, 50, 10, 0.1)


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(connect(tmp_path / "db.sqlite"))


def types(plan) -> list[str]:
    return [p.type for p in plan.proposals]


def test_bad_release_is_rolled_back_with_a_reconciling_pr(audit: AuditLog) -> None:
    llm = ScriptedLLM([proposal("rollback_deployment", to_revision=6), proposal("open_pr")])

    plan = Planner(llm, audit).plan(diagnosis("bad_release"))

    assert types(plan) == ["rollback_deployment", "open_pr"]
    assert plan.dropped == []
    assert (
        "bad_release on Deployment frontend: escalate, open_pr, rollback_deployment"
        in (llm.prompts[0])
    )


def test_low_confidence_escalates_without_asking_the_model(audit: AuditLog) -> None:
    llm = ScriptedLLM()

    plan = Planner(llm, audit).plan(diagnosis("bad_release", confidence=0.6))

    assert types(plan) == ["escalate"]
    assert "below 0.8" in plan.reason
    assert llm.prompts == []


@pytest.mark.parametrize(
    "category", ["network_degradation", "disk_pressure", "node_problem", "missing_dependency"]
)
def test_causes_with_no_safe_fix_escalate_without_the_model(audit: AuditLog, category) -> None:
    llm = ScriptedLLM()

    plan = Planner(llm, audit).plan(diagnosis(category))

    assert types(plan) == ["escalate"]
    assert llm.prompts == []


def test_nothing_wrong_means_no_action(audit: AuditLog) -> None:
    plan = Planner(ScriptedLLM(), audit).plan(diagnosis("no_action_needed"))
    assert types(plan) == ["none"]


def test_acting_on_a_symptom_escalates_the_whole_plan(audit: AuditLog) -> None:
    """Scenario s16: restarting cartservice when Redis is the problem."""
    llm = ScriptedLLM([proposal("scale_deployment", name="redis-cart", replicas=1),
                       proposal("restart_deployment", name="cartservice")])  # fmt: skip

    plan = Planner(llm, audit).plan(diagnosis("dependency_down", name="redis-cart"))

    assert types(plan) == ["escalate"]
    assert "cartservice is not a resource named in the diagnosis" in plan.reason
    assert len(plan.dropped) == 1


def test_action_type_not_allowed_for_the_cause_is_dropped(audit: AuditLog) -> None:
    """A bad env var cannot be fixed by a rollback or a restart."""
    llm = ScriptedLLM([proposal("rollback_deployment", name="cartservice", to_revision=3)])

    plan = Planner(llm, audit).plan(diagnosis("bad_config", name="cartservice"))

    assert types(plan) == ["escalate"]
    assert "rollback_deployment is not an allowed action for bad_config" in plan.reason


def test_two_causes_each_get_their_own_allowed_actions(audit: AuditLog) -> None:
    both = diagnosis("resource_limits_too_low", name="adservice",
                     also=[("bad_image_reference", "paymentservice")])  # fmt: skip
    patch = proposal(
        "patch_resources", name="adservice", container="server", limits={"memory": "256Mi"}
    )
    llm = ScriptedLLM(
        [patch, proposal("rollback_deployment", name="paymentservice", to_revision=2)]
    )

    assert types(Planner(llm, audit).plan(both)) == ["patch_resources", "rollback_deployment"]

    swapped = ScriptedLLM([proposal("rollback_deployment", name="adservice", to_revision=2)])
    assert types(Planner(swapped, audit).plan(both)) == ["escalate"]


def test_missing_parameters_are_caught(audit: AuditLog) -> None:
    llm = ScriptedLLM([proposal("scale_deployment", name="redis-cart")])

    plan = Planner(llm, audit).plan(diagnosis("dependency_down", name="redis-cart"))

    assert types(plan) == ["escalate"]
    assert "missing its parameters" in plan.reason


def test_none_is_refused_when_something_is_wrong(audit: AuditLog) -> None:
    llm = ScriptedLLM([proposal("none", name=None, summary="looks fine to me")])

    assert types(Planner(llm, audit).plan(diagnosis("bad_release"))) == ["escalate"]


def test_unusable_reply_is_retried_once_then_escalates(audit: AuditLog) -> None:
    good = ScriptedLLM("not json", [proposal("open_pr")])
    assert types(Planner(good, audit).plan(diagnosis("bad_release"))) == ["open_pr"]

    bad = ScriptedLLM("not json", "{}")
    assert types(Planner(bad, audit).plan(diagnosis("bad_release"))) == ["escalate"]


def test_model_outage_and_empty_plan_escalate(audit: AuditLog) -> None:
    down = ScriptedLLM(LLMError("connection refused"))
    assert types(Planner(down, audit).plan(diagnosis("bad_release"))) == ["escalate"]

    empty = ScriptedLLM([])
    assert types(Planner(empty, audit).plan(diagnosis("bad_release"))) == ["escalate"]


def test_plan_and_dropped_proposals_are_audited(audit: AuditLog) -> None:
    llm = ScriptedLLM([proposal("restart_deployment", name="emailservice")])

    Planner(llm, audit).plan(diagnosis("bad_release"))

    event = [e for e in audit.events("inc_1") if e.event_type == "proposal"][-1]
    assert event.payload["proposals"][0]["type"] == "escalate"
    assert event.payload["dropped"][0]["proposal"]["name"] == "emailservice"
    assert audit.verify_chain() is None


def test_every_category_has_an_entry_and_can_always_escalate() -> None:
    assert set(ALLOWED) == set(RootCauseCategory)
    for category, allowed in ALLOWED.items():
        assert ActionType.ESCALATE in allowed, category
    # Causes the actor identity cannot fix (env, probes) never allow a cluster write.
    for category in (RootCauseCategory.BAD_CONFIG, RootCauseCategory.BAD_PROBE_CONFIG):
        assert ALLOWED[category] == {ActionType.OPEN_PR, ActionType.ESCALATE}


def test_check_and_describe() -> None:
    ok = ActionProposal.model_validate(proposal("rollback_deployment", to_revision=6))
    assert check(ok, diagnosis("bad_release")) is None
    assert check(ok, diagnosis("bad_release", name="cartservice")) is not None

    assert describe(Plan([ok])) == (
        'rollback_deployment {"name": "frontend", "namespace": "boutique", "to_revision": 6}'
    )
