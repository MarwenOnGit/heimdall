"""Scenario s15, replayed without a cluster: a forced wrong fix is undone."""

from pathlib import Path

import pytest

from chaos.model import load_scenario
from chaos.params import sample
from heimdall.agent.planner import ForcedPlanner, check
from heimdall.core.models import IncidentState
from heimdall.report.postmortem import build, render_markdown
from tests.unit.test_pipeline import OPA, World

pytestmark = pytest.mark.skipif(not OPA.exists(), reason="run infra/tools.sh to download opa")

S15 = Path(__file__).parents[2] / "chaos/scenarios/dev/s15.yaml"


def forced_world(tmp_path: Path, recovers: list[bool]) -> tuple[World, ForcedPlanner]:
    scenario = load_scenario(S15)
    rendered = scenario.render(sample(scenario.parameters, seed=1))
    world = World(tmp_path, [], live=True, recovers=recovers)
    planner = ForcedPlanner(rendered.forced_plan, world.audit)
    world.pipeline._planner = planner  # what HEIMDALL_FORCED_PLAN does at startup
    return world, planner


def test_the_real_planner_would_never_propose_the_forced_fix(tmp_path: Path) -> None:
    world, planner = forced_world(tmp_path, [])
    diagnosis = world.diagnosed("bad_release", "frontend")
    (restart,) = planner.plan(diagnosis).proposals
    assert (
        check(restart, diagnosis) == "restart_deployment is not an allowed action for bad_release"
    )


def test_a_forced_fix_that_does_not_help_is_undone_and_escalated(tmp_path: Path) -> None:
    world, _ = forced_world(tmp_path, recovers=[False])
    diagnosis = world.diagnosed("bad_release", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert world.llm.calls == 0  # the model was not asked to plan
    (step,) = result.steps
    assert (step[0].type, step[1].decision, step[2].status) == (
        "restart_deployment", "allow", "undone")  # fmt: skip
    assert result.rolled_back
    assert world.state(diagnosis) is IncidentState.ESCALATED
    assert len(world.verifier.checked) == 1
    assert world.sent[-1][0].startswith("Escalated:")

    report = build(diagnosis.incident_id, world.audit.events(diagnosis.incident_id))
    assert report.actions[0].undone
    assert "FORCED by the benchmark harness" in render_markdown(report)


def test_a_forced_plan_still_answers_to_the_policy(tmp_path: Path) -> None:
    world, planner = forced_world(tmp_path, [])
    world.actions.set_frozen(True)
    diagnosis = world.diagnosed("bad_release", "frontend")

    result = world.pipeline.remediate(diagnosis)

    assert result.steps[0][1].decision == "deny"
    assert world.kube.patches == []


def test_forced_plan_is_read_from_the_environment_format(tmp_path: Path) -> None:
    world, planner = forced_world(tmp_path, [])
    raw = '{"proposals": [{"type": "escalate", "justification": "j", "expected_effect": "e"}]}'
    forced = ForcedPlanner.from_json(raw, world.audit)
    diagnosis = world.diagnosed("bad_release", "frontend")
    assert [p.type for p in forced.plan(diagnosis).proposals] == ["escalate"]
    event = list(world.audit.events(diagnosis.incident_id))[-1]
    assert event.payload["forced"] is True and event.actor == "benchmark"
