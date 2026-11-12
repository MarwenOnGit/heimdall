"""Benchmark metrics. Pure functions over run results; no cluster, no model.

Small samples are the normal case here (a run takes ten minutes), so every
rate comes with an interval and nothing is ever reported as a bare "100%".
"""

import math
from collections import defaultdict
from typing import Any

Z95 = 1.959964


def wilson(successes: int, runs: int, z: float = Z95) -> tuple[float, float]:
    """95% Wilson score interval for a proportion. (0, 1) when there is no data."""
    if runs == 0:
        return (0.0, 1.0)
    p = successes / runs
    denominator = 1 + z * z / runs
    centre = (p + z * z / (2 * runs)) / denominator
    margin = z * math.sqrt(p * (1 - p) / runs + z * z / (4 * runs * runs)) / denominator
    # The exact bounds at 0 and n are 0 and 1; rounding must not push past them.
    low = 0.0 if successes == 0 else max(0.0, centre - margin)
    high = 1.0 if successes == runs else min(1.0, centre + margin)
    return (low, high)


def pass_k(runs_by_scenario: dict[str, list[bool]], k: int) -> float | None:
    """How likely k runs of the same scenario are all correct, averaged over
    scenarios. An agent that is right 80% of the time on everything scores
    0.8**k: reliability, not only the average."""
    rates = [sum(r) / len(r) for r in runs_by_scenario.values() if r]
    if not rates:
        return None
    return sum(rate**k for rate in rates) / len(rates)


def expected_calibration_error(pairs: list[tuple[float, bool]], bins: int = 10) -> float | None:
    """Gap between stated confidence and actual correctness, weighted by how
    many answers fall in each confidence bin. 0 is perfectly calibrated."""
    if not pairs:
        return None
    buckets: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for confidence, correct in pairs:
        buckets[min(int(confidence * bins), bins - 1)].append((confidence, correct))
    error = 0.0
    for bucket in buckets.values():
        mean_confidence = sum(c for c, _ in bucket) / len(bucket)
        accuracy = sum(ok for _, ok in bucket) / len(bucket)
        error += len(bucket) / len(pairs) * abs(mean_confidence - accuracy)
    return error


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def run_facts(result: dict[str, Any]) -> dict[str, Any]:
    """The numbers one run contributes, whichever agent produced it."""
    grade = result.get("grade") or {}
    actions = result.get("action_grade") or {}
    investigations = [i.get("result") or {} for i in result.get("incidents", [])]
    confidences = [
        cause["confidence"]
        for inv in investigations
        for cause in (inv.get("diagnosis") or {}).get("root_causes", [])
        if "confidence" in cause
    ]
    return {
        "scenario": result["scenario"],
        "correct": bool(grade.get("correct")),
        "unknown": bool(grade.get("answered_unknown")),
        "unsafe": bool(actions.get("unsafe")),
        "would_be_unsafe": bool(actions.get("would_be_unsafe")),
        "abstention_expected": bool(actions.get("abstention_expected")),
        "abstained": bool(actions.get("abstained")),
        "fix_expected": "action_grade" in result and not actions.get("abstention_expected"),
        "fix_acceptable": bool(actions.get("acceptable_fix")),
        "confidence": min(confidences) if confidences else None,
        "seconds": sum(inv.get("seconds", 0) for inv in investigations) or None,
        "tokens": sum(
            inv.get("prompt_tokens", 0) + inv.get("completion_tokens", 0) for inv in investigations
        )
        or None,
        "tool_calls": sum(inv.get("tool_calls", 0) for inv in investigations) or None,
    }


def _rate(successes: int, runs: int) -> dict[str, Any]:
    low, high = wilson(successes, runs)
    return {"n": successes, "of": runs, "rate": successes / runs if runs else None,
            "low": low, "high": high}  # fmt: skip


def summarize(results: list[dict[str, Any]], k: int = 5) -> dict[str, Any]:
    """Everything the results page shows for one configuration."""
    facts = [run_facts(r) for r in results if "error" not in r and "grade" in r]
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        by_scenario[fact["scenario"]].append(fact)

    abstain = [f for f in facts if f["abstention_expected"]]
    fixes = [f for f in facts if f["fix_expected"]]
    return {
        "runs": len(facts),
        "skipped": len(results) - len(facts),
        "diagnosis": _rate(sum(f["correct"] for f in facts), len(facts)),
        "unknown": _rate(sum(f["unknown"] for f in facts), len(facts)),
        "unsafe": _rate(sum(f["unsafe"] for f in facts), len(facts)),
        "would_be_unsafe": _rate(sum(f["would_be_unsafe"] for f in facts), len(facts)),
        "abstention": _rate(sum(f["abstained"] for f in abstain), len(abstain)),
        "acceptable_fix": _rate(sum(f["fix_acceptable"] for f in fixes), len(fixes)),
        "pass_k": pass_k({s: [f["correct"] for f in fs] for s, fs in by_scenario.items()}, k),
        "k": k,
        "ece": expected_calibration_error(
            [(f["confidence"], f["correct"]) for f in facts if f["confidence"] is not None]
        ),
        "mean_seconds": _mean([f["seconds"] for f in facts if f["seconds"]]),
        "mean_tokens": _mean([f["tokens"] for f in facts if f["tokens"]]),
        "p95_tokens": _p95([f["tokens"] for f in facts if f["tokens"]]),
        "scenarios": {
            scenario: {
                "diagnosis": _rate(sum(f["correct"] for f in fs), len(fs)),
                "unknown": sum(f["unknown"] for f in fs),
                "unsafe": sum(f["unsafe"] for f in fs),
            }
            for scenario, fs in sorted(by_scenario.items())
        },
    }
