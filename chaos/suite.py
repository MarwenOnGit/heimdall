"""Run many scenarios, several seeds each, and summarize how reliable they are.

    uv run python -m chaos.suite --runs 10            # every dev scenario
    uv run python -m chaos.suite s05 s07 --runs 3

A scenario is reliable when its expected alerts fire and the cluster returns
to baseline afterwards, run after run. Completed runs are skipped on restart,
so a long batch can be interrupted and resumed.
"""

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chaos.model import load_all
from chaos.run import RESULTS, SCENARIOS, log, run


def run_ok(result: dict[str, Any]) -> bool:
    return (
        "error" not in result
        and bool(result.get("alerts_ok"))
        and bool(result.get("baseline_restored"))
    )


def load_results(directory: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """Latest result per (scenario, seed)."""
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        result = json.loads(path.read_text())
        if "scenario" in result and "seed" in result:
            latest[(result["scenario"], result["seed"])] = result
    return latest


def summarize(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per scenario: runs, how many were ok, and time to alert."""
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_scenario[result["scenario"]].append(result)

    rows = []
    for scenario in sorted(by_scenario):
        runs = by_scenario[scenario]
        times = [
            max(r["alerts_fired_after_seconds"].values())
            for r in runs
            if r.get("alerts_ok") and r.get("alerts_fired_after_seconds")
        ]
        rows.append(
            {
                "scenario": scenario,
                "runs": len(runs),
                "ok": sum(run_ok(r) for r in runs),
                "alerts_ok": sum(bool(r.get("alerts_ok")) for r in runs),
                "baseline_restored": sum(bool(r.get("baseline_restored")) for r in runs),
                "median_seconds_to_alert": sorted(times)[len(times) // 2] if times else None,
                "graded": sum("grade" in r for r in runs),
                "diagnosed_correctly": sum(bool(r.get("grade", {}).get("correct")) for r in runs),
            }
        )
    return rows


def format_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Scenario | Runs | OK | Alerts fired | Baseline restored | Median time to alert "
        "| Diagnosed correctly |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        seconds = r["median_seconds_to_alert"]
        time_to_alert = f"{seconds:.0f} s" if seconds is not None else "n/a"
        diagnosed = f"{r['diagnosed_correctly']}/{r['graded']}" if r["graded"] else "n/a"
        lines.append(
            f"| {r['scenario']} | {r['runs']} | {r['ok']} | {r['alerts_ok']} "
            f"| {r['baseline_restored']} | {time_to_alert} | {diagnosed} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenarios", nargs="*", help="scenario ids; default is all")
    parser.add_argument("--runs", type=int, default=10, help="seeds per scenario, 1..N")
    parser.add_argument("--set", dest="scenario_set", default="dev", choices=["dev", "holdout"])
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--agent", action="store_true", help="wait for and grade Heimdall")
    parser.add_argument("--results", type=Path, default=RESULTS, help="result directory")
    args = parser.parse_args()

    scenarios = [
        s
        for s in load_all(SCENARIOS / args.scenario_set)
        if not args.scenarios or s.id in args.scenarios
    ]
    results_dir: Path = args.results
    results_dir.mkdir(parents=True, exist_ok=True)

    # Seeds go round-robin across scenarios, so the "10 times in a row" check
    # also covers every scenario following every other one.
    for seed in range(1, args.runs + 1):
        for scenario in scenarios:
            if args.summary_only or (scenario.id, seed) in load_results(results_dir):
                continue
            try:
                result = run(scenario, seed, keep=False, agent=args.agent)
            except Exception as e:  # noqa: BLE001 - one broken run must not stop the batch
                result = {
                    "scenario": scenario.id,
                    "seed": seed,
                    "error": f"{type(e).__name__}: {e}",
                }
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            path = results_dir / f"{scenario.id}-seed{seed}-{stamp}.json"
            path.write_text(json.dumps(result, indent=2) + "\n")
            log(f"{scenario.id} seed={seed} ok={run_ok(result)}")

    wanted = {s.id for s in scenarios}
    results = [r for (sid, _), r in load_results(results_dir).items() if sid in wanted]
    rows = summarize(results)
    print(format_table(rows))
    return 0 if all(r["ok"] == r["runs"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
