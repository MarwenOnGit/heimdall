import json
from pathlib import Path

from chaos.suite import format_table, load_results, run_ok, summarize


def result(scenario: str, seed: int, *, alerts: bool = True, baseline: bool = True, t: float = 60):
    return {
        "scenario": scenario,
        "seed": seed,
        "alerts_ok": alerts,
        "alerts_fired_after_seconds": {"X/y": t} if alerts else {},
        "baseline_restored": baseline,
    }


def test_run_ok_needs_alerts_and_baseline_and_no_error() -> None:
    assert run_ok(result("s01", 1))
    assert not run_ok(result("s01", 1, alerts=False))
    assert not run_ok(result("s01", 1, baseline=False))
    assert not run_ok({**result("s01", 1), "error": "cluster not at baseline"})


def test_summarize_counts_per_scenario() -> None:
    rows = summarize(
        [
            result("s01", 1, t=40),
            result("s01", 2, t=80),
            result("s01", 3, alerts=False),
            result("s02", 1, baseline=False, t=10),
        ]
    )
    assert rows[0] == {
        "scenario": "s01",
        "runs": 3,
        "ok": 2,
        "alerts_ok": 2,
        "baseline_restored": 3,
        "median_seconds_to_alert": 80,
        "graded": 0,
        "diagnosed_correctly": 0,
    }
    assert rows[1]["ok"] == 0
    assert "| s01 | 3 | 2 | 2 | 3 | 80 s | n/a |" in format_table(rows)


def test_load_results_keeps_latest_per_scenario_and_seed(tmp_path: Path) -> None:
    (tmp_path / "s01-seed1-20260101T000000.json").write_text(
        json.dumps(result("s01", 1, alerts=False))
    )
    (tmp_path / "s01-seed1-20260102T000000.json").write_text(json.dumps(result("s01", 1)))
    (tmp_path / "s01-seed2-20260101T000000.json").write_text(json.dumps(result("s01", 2)))

    latest = load_results(tmp_path)

    assert set(latest) == {("s01", 1), ("s01", 2)}
    assert latest[("s01", 1)]["alerts_ok"] is True
