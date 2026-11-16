"""Run the benchmark: scenarios x seeds for one configuration, resumable.

    uv run python -m bench.run_bench --agent heimdall --runs 5 s04 s05
    uv run python -m bench.run_bench --agent baseline_rules --runs 5
    uv run python -m bench.run_bench --agent baseline_rules --offline --runs 10

    uv run python -m bench.run_bench --variant single-shot --runs 5

`--variant` runs Heimdall with one switch changed, for the comparisons on the
results page. The switch is set on the Deployment before the batch and removed
after it; each change restarts the pod.

Each run is one result file under bench/results/<config>/. A run that already
has a file is skipped, so a batch can be stopped and continued. One run takes
8 to 15 minutes and loads the machine; `--max-runs` stops a batch early.

`--offline` needs no cluster: it scores the rules baseline against the alerts
each scenario is expected to fire.
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench import baseline_rules
from chaos.model import Scenario, load_all

ROOT = Path(__file__).parents[1]
RESULTS = ROOT / "bench" / "results"
SCENARIOS = ROOT / "chaos" / "scenarios"


# Comparison name -> the environment that differs from the normal Heimdall.
VARIANTS: dict[str, dict[str, str]] = {
    # The gate records its verdict and lets the dry run through.
    "gate-off": {"HEIMDALL_POLICY_LOG_ONLY": "true"},
    # Same model, first-look observations, one answer, no tool loop.
    "single-shot": {"HEIMDALL_SINGLE_SHOT": "true"},
    "no-search": {"HEIMDALL_SEARCH_PAST_INCIDENTS": "false"},
}


def set_variant(variant: str, on: bool) -> None:
    """Switch the variant's environment on the Heimdall Deployment on or off."""
    from chaos import cluster

    changes = [f"{k}={v}" if on else f"{k}-" for k, v in VARIANTS[variant].items()]
    cluster.kubectl("-n", "heimdall", "set", "env", "deploy/heimdall", *changes)
    cluster.kubectl("-n", "heimdall", "rollout", "status", "deploy/heimdall", "--timeout=180s")


def done(directory: Path) -> set[tuple[str, int]]:
    finished = set()
    for path in directory.glob("*.json"):
        result = json.loads(path.read_text())
        if "error" not in result:
            finished.add((result["scenario"], result["seed"]))
    return finished


def write(directory: Path, result: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = directory / f"{result['scenario']}-seed{result['seed']}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    return path


def run_one(scenario: Scenario, seed: int, agent: str, scenario_set: str) -> dict[str, Any]:
    # Imported here: the runner talks to the cluster, and --offline must not.
    from chaos.run import run

    if agent == "heimdall":
        return run(scenario, seed, keep=False, agent=True, scenario_set=scenario_set)

    result = run(scenario, seed, keep=False)
    if "error" in result:
        return result
    fired = [*result.get("alerts_expected", []), *result.get("alerts_other", [])]
    alerts = [(a, None if s == "None" else s) for a, s in (f.split("/", 1) for f in fired)]
    scored = baseline_rules.score(
        scenario.id, seed, result["ground_truth"], result["acceptable_fixes"], alerts
    )
    return {**result, **scored}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenarios", nargs="*", help="scenario ids; default: all in the set")
    parser.add_argument("--agent", choices=["heimdall", "baseline_rules"], default="heimdall")
    parser.add_argument("--set", dest="scenario_set", default="dev", choices=["dev", "holdout"])
    parser.add_argument("--runs", type=int, default=5, help="seeds per scenario")
    parser.add_argument("--seed", type=int, default=1, help="first seed")
    parser.add_argument("--variant", choices=sorted(VARIANTS), help="a heimdall comparison run")
    parser.add_argument("--config", help="name of this configuration; default: <agent>-<set>")
    parser.add_argument("--offline", action="store_true", help="baseline_rules without a cluster")
    parser.add_argument("--max-runs", type=int, help="stop after this many new runs")
    args = parser.parse_args()

    scenarios = [
        s
        for s in load_all(SCENARIOS / args.scenario_set)
        if not args.scenarios or s.id in args.scenarios
    ]
    seeds = list(range(args.seed, args.seed + args.runs))
    if args.variant and (args.agent != "heimdall" or args.offline):
        parser.error("--variant only works with --agent heimdall on the cluster")
    suffix = f"-{args.variant}" if args.variant else "-offline" if args.offline else ""
    config = args.config or f"{args.agent}-{args.scenario_set}{suffix}"
    directory = RESULTS / config

    if args.offline:
        if args.agent != "baseline_rules":
            parser.error("--offline only works with --agent baseline_rules")
        for path in directory.glob("*.json"):
            path.unlink()  # offline scoring is instant and deterministic: redo it all
        for result in baseline_rules.score_offline(scenarios, seeds):
            write(directory, result)
        print(f"offline results written to {directory}")
        return 0

    finished = done(directory)
    todo = [
        (scenario, seed)
        for seed in seeds
        for scenario in scenarios
        if (scenario.id, seed) not in finished
        # A forced plan replaces the model, so a variant of the model changes nothing.
        and not (args.variant and scenario.forced_plan)
    ]
    if args.max_runs is not None:
        todo = todo[: args.max_runs]
    if not todo:
        print(f"nothing to run; {len(finished)} were already done")
        return 0

    if args.variant:
        set_variant(args.variant, on=True)
    try:
        for scenario, seed in todo:
            result = run_one(scenario, seed, args.agent, args.scenario_set)
            result.update(agent=args.agent, config=config)
            print(f"{scenario.id} seed {seed}: written to {write(directory, result).name}")
    finally:
        if args.variant:
            set_variant(args.variant, on=False)
    print(f"{len(todo)} new runs; {len(finished)} were already done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
