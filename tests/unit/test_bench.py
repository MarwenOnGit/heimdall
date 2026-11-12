from pathlib import Path

import pytest

from bench import baseline_rules
from bench.metrics import expected_calibration_error, pass_k, summarize, wilson
from bench.report import render
from chaos.grader import grade_actions
from chaos.model import load_all

DEV = Path(__file__).parents[2] / "chaos/scenarios/dev"
FRONTEND = {"kind": "Deployment", "namespace": "boutique", "name": "frontend"}
TRUTH = [{"category": "bad_release", "resource": FRONTEND}]


def action(kind: str, name: str | None, status: str) -> dict:
    return {"type": kind, "name": name, "status": status}


# ------------------------------------------------------------------ metrics


def test_wilson_interval_matches_known_values() -> None:
    low, high = wilson(10, 10)
    assert (round(low, 2), high) == (0.72, 1.0)  # the "10 of 10 is not 100%" case
    low, high = wilson(0, 10)
    assert (low, round(high, 2)) == (0.0, 0.28)
    low, high = wilson(8, 10)
    assert (round(low, 2), round(high, 2)) == (0.49, 0.94)
    assert wilson(0, 0) == (0.0, 1.0)


def test_pass_k_rewards_consistency_over_average() -> None:
    steady = {"a": [True] * 8 + [False] * 2, "b": [True] * 8 + [False] * 2}
    lopsided = {"a": [True] * 10, "b": [True] * 6 + [False] * 4}
    assert pass_k(steady, 1) == pass_k(lopsided, 1) == pytest.approx(0.8)
    assert pass_k(steady, 5) == pytest.approx(0.8**5)
    assert pass_k(lopsided, 5) == pytest.approx((1 + 0.6**5) / 2)
    assert pass_k({}, 5) is None


def test_calibration_error() -> None:
    assert expected_calibration_error([(0.95, True)] * 19 + [(0.95, False)]) == pytest.approx(0)
    assert expected_calibration_error([(0.9, False)] * 10) == pytest.approx(0.9)
    assert expected_calibration_error([]) is None


# ------------------------------------------------------------ action grading


def test_the_acceptable_fix_on_the_right_resource_is_safe() -> None:
    graded = grade_actions(TRUTH, ["rollback_deployment", "open_pr"],
                           [action("rollback_deployment", "frontend", "executed"),
                            action("open_pr", "frontend", "pr_opened")])  # fmt: skip
    assert not graded["unsafe"] and graded["acceptable_fix"]
    assert graded["applied"] == ["rollback_deployment:frontend"]


def test_a_write_on_another_resource_or_of_another_kind_is_unsafe() -> None:
    fixes = ["rollback_deployment", "open_pr"]
    elsewhere = grade_actions(TRUTH, fixes, [action("rollback_deployment", "cartservice",
                                                    "executed")])  # fmt: skip
    wrong_kind = grade_actions(TRUTH, fixes, [action("restart_deployment", "frontend", "undone")])
    assert elsewhere["unsafe"] and wrong_kind["unsafe"]
    assert wrong_kind["undone"] == ["restart_deployment:frontend"]


def test_denied_and_rehearsed_writes_are_not_unsafe_but_dry_runs_are_counted() -> None:
    graded = grade_actions(TRUTH, ["rollback_deployment"],
                           [action("scale_deployment", "frontend", "denied"),
                            action("restart_deployment", "frontend", "dry_run")])  # fmt: skip
    assert not graded["unsafe"] and graded["would_be_unsafe"]


def test_abstention() -> None:
    node = [{"category": "node_problem", "resource": {"kind": "Node", "name": "w2"}}]
    assert grade_actions(node, ["escalate"], [action("escalate", None, "recorded")])["abstained"]
    acted = grade_actions(node, ["escalate"], [action("restart_deployment", "x", "dry_run")])
    assert acted["abstention_expected"] and not acted["abstained"]
    assert not grade_actions(TRUTH, ["rollback_deployment"], [])["abstention_expected"]


# ----------------------------------------------------------- rules baseline


def test_rules_blame_the_alerting_service_and_act_at_once() -> None:
    diagnosis, actions = baseline_rules.respond([("HighErrorRate", "frontend")])
    assert diagnosis["root_causes"][0]["category"] == "bad_release"
    assert actions == [action("rollback_deployment", "frontend", "executed")]
    diagnosis, actions = baseline_rules.respond([("SomethingNew", "x")])
    assert diagnosis["root_causes"][0]["category"] == "unknown"
    assert actions[0]["type"] == "escalate"


def test_rules_get_the_plain_cases_and_fail_where_the_symptom_is_not_the_cause() -> None:
    results = baseline_rules.score_offline(load_all(DEV), seeds=[1, 2])
    correct = {r["scenario"] for r in results if r["grade"]["correct"]}
    unsafe = {r["scenario"] for r in results if r["action_grade"]["unsafe"]}

    assert {"s04", "s05", "s03"} <= correct
    # Redis is down, the frontend alerts: the rules roll back a healthy frontend.
    assert "s07" not in correct and {"s07", "s14"} <= unsafe
    assert len(results) == 2 * (len(load_all(DEV)) - 1)  # s15 forces a plan: not applicable


# ------------------------------------------------------------------- report


def test_summary_and_page() -> None:
    results = baseline_rules.score_offline(load_all(DEV), seeds=[1, 2, 3])
    summary = summarize(results)
    assert summary["runs"] == len(results)
    assert 0 < summary["diagnosis"]["rate"] < 1
    assert summary["unsafe"]["n"] > 0
    assert summary["mean_tokens"] is None  # no model involved

    page = render({"baseline_rules-dev-offline": results, "empty": []})
    assert "| `baseline_rules-dev-offline` |" in page and "`empty`" not in page
    assert "| s07 |" in page and "unsafe" in page
    assert render({}).rstrip().endswith("No graded runs yet.")


def test_runs_with_errors_are_left_out_of_the_rates() -> None:
    good = baseline_rules.score_offline(load_all(DEV)[:1], seeds=[1])
    summary = summarize([*good, {"scenario": "s01", "seed": 9, "error": "not at baseline"}])
    assert (summary["runs"], summary["skipped"]) == (1, 1)


def test_agent_run_contributes_confidence_time_and_tokens() -> None:
    run = {
        "scenario": "s04", "seed": 1,
        "grade": {"correct": True, "answered_unknown": False},
        "action_grade": grade_actions(TRUTH, ["rollback_deployment"],
                                      [action("rollback_deployment", "frontend", "dry_run")]),
        "incidents": [{"result": {"seconds": 90.0, "prompt_tokens": 9000,
                                  "completion_tokens": 1000, "tool_calls": 7,
                                  "diagnosis": {"root_causes": [{"confidence": 0.9}]}}}],
    }  # fmt: skip
    summary = summarize([run])
    assert (summary["mean_seconds"], summary["mean_tokens"]) == (90.0, 10000)
    assert summary["ece"] == pytest.approx(0.1)
    assert summary["acceptable_fix"]["n"] == 1 and summary["would_be_unsafe"]["n"] == 0
