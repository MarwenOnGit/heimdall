from pathlib import Path

import pytest
from pydantic import ValidationError

from chaos.model import Scenario, load_all
from chaos.params import MAX_OFFSET_SECONDS, sample
from heimdall.core.models import RootCauseCategory

SCENARIO_DIR = Path(__file__).parents[2] / "chaos" / "scenarios" / "dev"

MINIMAL = {
    "id": "t01",
    "description": "lower the memory limit of {service}",
    "difficulty": "easy",
    "parameters": {
        "service": {"choice": ["cartservice", "adservice"]},
        "memory_mi": {"int_range": [32, 48]},
    },
    "inject": [
        {
            "type": "git_commit",
            "message": "tune {service} memory",
            "files": {"online-boutique/overrides/{service}.yaml": "memory: {memory_mi}Mi"},
            "patches": ["overrides/{service}.yaml"],
        }
    ],
    "expected_alerts": [{"alertname": "ContainerOOMKilled", "service": "{service}"}],
    "ground_truth": [
        {
            "category": "resource_limits_too_low",
            "resource": {"kind": "Deployment", "namespace": "boutique", "name": "{service}"},
        }
    ],
    "acceptable_fixes": ["patch_resources", "open_pr"],
    "recovery_condition": ['up{{service="{service}"}} == 1'],
}


def test_render_fills_placeholders_everywhere() -> None:
    scenario = Scenario.model_validate(MINIMAL)
    rendered = scenario.render({"service": "adservice", "memory_mi": 40, "offset_seconds": 3})

    commit = rendered.inject[0]
    assert commit.type == "git_commit"
    assert commit.message == "tune adservice memory"
    assert commit.files == {"online-boutique/overrides/adservice.yaml": "memory: 40Mi"}
    assert rendered.expected_alerts[0].service == "adservice"
    assert rendered.ground_truth[0].resource is not None
    assert rendered.ground_truth[0].resource.name == "adservice"
    assert rendered.recovery_condition == ['up{service="adservice"} == 1']


def test_render_rejects_missing_parameter() -> None:
    scenario = Scenario.model_validate(MINIMAL)
    with pytest.raises(ValueError, match="missing parameters"):
        scenario.render({"service": "adservice"})


def test_render_rejects_unknown_placeholder() -> None:
    data = {**MINIMAL, "description": "break {nonexistent}"}
    scenario = Scenario.model_validate(data)
    with pytest.raises(ValueError, match="unknown placeholder"):
        scenario.render({"service": "adservice", "memory_mi": 40})


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate({**MINIMAL, "expected_alert": []})


def test_ground_truth_needs_resource_unless_nothing_is_wrong() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate({**MINIMAL, "ground_truth": [{"category": "bad_release"}]})
    ok = Scenario.model_validate(
        {
            **MINIMAL,
            "ground_truth": [{"category": "no_action_needed"}],
            "acceptable_fixes": ["none"],
        }
    )
    assert ok.ground_truth[0].category is RootCauseCategory.NO_ACTION_NEEDED


def test_sampling_is_deterministic_and_in_range() -> None:
    scenario = Scenario.model_validate(MINIMAL)
    first = sample(scenario.parameters, seed=7)
    assert first == sample(scenario.parameters, seed=7)
    assert first["service"] in {"cartservice", "adservice"}
    assert 32 <= first["memory_mi"] <= 48
    assert 0 <= first["offset_seconds"] <= MAX_OFFSET_SECONDS


def test_sampling_varies_with_seed() -> None:
    scenario = Scenario.model_validate(MINIMAL)
    seen = {sample(scenario.parameters, seed=s)["memory_mi"] for s in range(50)}
    assert len(seen) > 5


def test_adding_a_parameter_keeps_existing_samples() -> None:
    scenario = Scenario.model_validate(MINIMAL)
    extended = Scenario.model_validate(
        {**MINIMAL, "parameters": {**MINIMAL["parameters"], "extra": {"int_range": [1, 9]}}}
    )
    before = sample(scenario.parameters, seed=11)
    after = sample(extended.parameters, seed=11)
    assert before["service"] == after["service"]
    assert before["memory_mi"] == after["memory_mi"]


def test_every_dev_scenario_loads_and_renders() -> None:
    for scenario in load_all(SCENARIO_DIR):
        for seed in range(5):
            scenario.render(sample(scenario.parameters, seed))
