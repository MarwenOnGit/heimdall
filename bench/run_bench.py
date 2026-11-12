"""Run the benchmark: scenarios x seeds for one configuration, resumable.

    uv run python -m bench.run_bench --agent heimdall --runs 5 s04 s05
    uv run python -m bench.run_bench --agent baseline_rules --runs 5
    uv run python -m bench.run_bench --agent baseline_rules --offline --runs 10

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
    config = args.config or f"{args.agent}-{args.scenario_set}{'-offline' if args.offline else ''}"
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
    new = 0
    for seed in seeds:
        for scenario in scenarios:
            if (scenario.id, seed) in finished:
                continue
            if args.max_runs is not None and new >= args.max_runs:
                print(f"stopped after {new} runs (--max-runs)")
                return 0
            result = run_one(scenario, seed, args.agent, args.scenario_set)
            result.update(agent=args.agent, config=config)
            print(f"{scenario.id} seed {seed}: written to {write(directory, result).name}")
            new += 1
    print(f"{new} new runs; {len(finished)} were already done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
