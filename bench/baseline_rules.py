"""The rules-only baseline: a fixed table from alert name to cause and action.

This is what a team writes before reaching for a model. It never looks at the
cluster; it reads the alert and reacts. It is the comparison that shows what
investigating buys: the table was written from the alert rules alone, without
looking at the scenarios.

Because it needs nothing but alerts, it can also be scored offline, against
the alerts each scenario is expected to fire. That is its best case: in a real
run more alerts fire than the expected ones.
"""

from typing import Any

from chaos.grader import grade, grade_actions
from chaos.model import Scenario
from chaos.params import sample

# alert name -> (root cause category, action). The resource is always the
# Deployment of the alerting service: a rule cannot look any further.
RULES: dict[str, tuple[str, str]] = {
    "PodCrashLooping": ("bad_config", "restart_deployment"),
    "ContainerOOMKilled": ("resource_limits_too_low", "patch_resources"),
    "ImagePullFailing": ("bad_image_reference", "rollback_deployment"),
    "PodNotReady": ("bad_probe_config", "restart_deployment"),
    "PodPendingConfig": ("bad_config", "escalate"),
    "DeploymentReplicasMismatch": ("dependency_down", "scale_deployment"),
    "CPUThrottlingHigh": ("resource_saturation", "scale_deployment"),
    "HighErrorRate": ("bad_release", "rollback_deployment"),
    "HighLatencyP95": ("resource_saturation", "scale_deployment"),
    "VolumeAlmostFull": ("disk_pressure", "escalate"),
    "NodeNotReady": ("node_problem", "escalate"),
}
DEFAULT = ("unknown", "escalate")


def respond(alerts: list[tuple[str, str | None]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The diagnosis and the actions the rules produce for a set of alerts.

    The baseline has no policy gate and no dry run: what it decides, it does.
    """
    causes, actions = [], []
    for alertname, service in sorted(alerts, key=lambda a: (a[0], a[1] or "")):
        category, action = RULES.get(alertname, DEFAULT)
        resource = (
            {"kind": "Deployment", "namespace": "boutique", "name": service} if service else None
        )
        causes.append({"category": category, "affected_resource": resource, "confidence": 1.0})
        status = "recorded" if action == "escalate" else "executed"
        actions.append({"type": action, "name": service, "status": status})
    return {"root_causes": causes}, actions


def score(
    scenario_id: str,
    seed: int,
    ground_truth: list[dict[str, Any]],
    acceptable_fixes: list[str],
    alerts: list[tuple[str, str | None]],
) -> dict[str, Any]:
    diagnosis, actions = respond(alerts)
    return {
        "scenario": scenario_id,
        "seed": seed,
        "agent": "baseline_rules",
        "ground_truth": ground_truth,
        "acceptable_fixes": acceptable_fixes,
        "alerts": sorted(f"{a}/{s}" for a, s in alerts),
        "grade": grade(ground_truth, diagnosis),
        "action_grade": grade_actions(ground_truth, acceptable_fixes, actions),
    }


def score_offline(scenarios: list[Scenario], seeds: list[int]) -> list[dict[str, Any]]:
    """Score the rules against each scenario's expected alerts, per seed."""
    results = []
    for scenario in scenarios:
        if scenario.forced_plan:
            continue  # about what Heimdall does after a forced plan, not about diagnosis
        for seed in seeds:
            rendered = scenario.render(sample(scenario.parameters, seed))
            result = score(
                scenario.id,
                seed,
                [g.model_dump(mode="json") for g in rendered.ground_truth],
                [str(fix) for fix in rendered.acceptable_fixes],
                [(a.alertname, a.service) for a in rendered.expected_alerts],
            )
            results.append({**result, "offline": True})
    return results
