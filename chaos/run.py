"""Run one chaos scenario against the lab cluster.

    uv run python -m chaos.run s05 --seed 3

Steps: check the cluster is at baseline, sample parameters from the seed,
inject the fault, wait for the expected alerts, record what happened, restore
the baseline, and write a JSON result to bench/results/.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chaos import cluster
from chaos.model import GitCommit, Kubectl, Manifest, PauseNode, Scenario, Step, Wait, load_all
from chaos.params import sample

SCENARIOS = Path(__file__).parent / "scenarios"
RESULTS = cluster.ROOT / "bench" / "results"
# Scenario commits are made as a developer, not as a bot, so they look like
# the ordinary changes Heimdall must tell apart from the harmful one.
COMMIT_AUTHOR = "dev-alice"


def log(message: str) -> None:
    print(f"[{datetime.now(UTC):%H:%M:%S}] {message}", file=sys.stderr, flush=True)


def find_scenario(scenario_id: str, scenario_set: str) -> Scenario:
    for scenario in load_all(SCENARIOS / scenario_set):
        if scenario.id == scenario_id:
            return scenario
    raise SystemExit(f"no scenario {scenario_id!r} in set {scenario_set!r}")


def apply_step(step: Step) -> dict[str, Any]:
    """Run one injection step and return what was done, for the result file."""
    if isinstance(step, GitCommit):
        with cluster.gitea() as git:
            sha = git.commit(step.message, step.files, step.patches, COMMIT_AUTHOR)
        return {"type": step.type, "commit": sha, "message": step.message}
    if isinstance(step, Kubectl):
        cluster.kubectl(*step.args)
        return {"type": step.type, "args": step.args}
    if isinstance(step, Manifest):
        cluster.kubectl("apply", "-f", "-", stdin=step.content)
        return {"type": step.type}
    if isinstance(step, PauseNode):
        subprocess.run(["docker", "pause", step.node], check=True, capture_output=True)
        return {"type": step.type, "node": step.node}
    if isinstance(step, Wait):
        time.sleep(step.seconds)
        return {"type": step.type, "seconds": step.seconds}
    raise TypeError(f"unknown step {step!r}")


def wait_for_alerts(
    expected: set[tuple[str, str | None]], timeout: float
) -> tuple[dict[str, float], set[tuple[str, str | None]]]:
    """Wait until every expected alert fires.

    Returns the seconds each expected alert took to fire, and every alert that
    was seen firing, expected or not.
    """
    start = time.monotonic()
    fired: dict[str, float] = {}
    seen: set[tuple[str, str | None]] = set()
    while time.monotonic() - start < timeout:
        now = cluster.firing_alerts()
        seen |= now
        for alert in expected & now:
            fired.setdefault(f"{alert[0]}/{alert[1]}", round(time.monotonic() - start, 1))
        if len(fired) == len(expected):
            break
        time.sleep(5)
    return fired, seen


def run(scenario: Scenario, seed: int, keep: bool) -> dict[str, Any]:
    params = sample(scenario.parameters, seed)
    rendered = scenario.render(params)
    result: dict[str, Any] = {
        "scenario": scenario.id,
        "seed": seed,
        "parameters": params,
        "ground_truth": [g.model_dump(mode="json") for g in rendered.ground_truth],
        "acceptable_fixes": list(rendered.acceptable_fixes),
        "started_at": datetime.now(UTC).isoformat(),
    }

    log(f"{scenario.id} seed={seed} params={params}")
    problems = cluster.wait_for_baseline(timeout=600)
    if problems:
        result["error"] = f"cluster not at baseline: {problems}"
        return result

    time.sleep(params["offset_seconds"])
    result["injected_at"] = datetime.now(UTC).isoformat()
    try:
        result["steps"] = [apply_step(step) for step in rendered.inject]
        log("injected, waiting for alerts")

        expected = {(a.alertname, a.service) for a in rendered.expected_alerts}
        fired, seen = wait_for_alerts(expected, rendered.alert_timeout_seconds)
        result["alerts_expected"] = sorted(f"{a}/{s}" for a, s in expected)
        result["alerts_fired_after_seconds"] = fired
        result["alerts_other"] = sorted(f"{a}/{s}" for a, s in seen - expected)
        result["alerts_ok"] = len(fired) == len(expected)
        log(f"alerts ok={result['alerts_ok']} fired={fired} other={result['alerts_other']}")
    finally:
        if not keep:
            cluster.reset()
            leftover = cluster.wait_for_baseline(timeout=600)
            result["baseline_restored"] = not leftover
            if leftover:
                result["baseline_problems"] = leftover
            log(f"baseline restored={not leftover}")
    result["finished_at"] = datetime.now(UTC).isoformat()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", help="scenario id, for example s05")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--set", dest="scenario_set", default="dev", choices=["dev", "holdout"])
    parser.add_argument("--keep", action="store_true", help="leave the fault in place")
    args = parser.parse_args()

    scenario = find_scenario(args.scenario, args.scenario_set)
    result = run(scenario, args.seed, args.keep)

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = RESULTS / f"{scenario.id}-seed{args.seed}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    log(f"result written to {path.relative_to(cluster.ROOT)}")

    ok = "error" not in result and result.get("alerts_ok") and result.get("baseline_restored", True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
