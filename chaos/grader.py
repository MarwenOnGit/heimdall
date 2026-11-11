"""Deterministic grading of a diagnosis against a scenario's ground truth.

No model is involved in grading. A diagnosis is correct when every true root
cause is matched by a reported one with the same category and the same
resource, and nothing else is blamed.
"""

from typing import Any


def _key(category: str, resource: dict[str, Any] | None) -> tuple[str, str | None, str | None]:
    if resource is None:
        return (category, None, None)
    return (category, resource.get("kind"), resource.get("name"))


def grade(ground_truth: list[dict[str, Any]], diagnosis: dict[str, Any] | None) -> dict[str, Any]:
    """Compare reported root causes with the true ones.

    Returns per-cause and overall correctness, plus whether the answer was a
    safe "unknown" (never correct, but never harmful either).
    """
    truth = {_key(g["category"], g.get("resource")) for g in ground_truth}
    causes = (diagnosis or {}).get("root_causes", [])
    reported = {_key(c["category"], c.get("affected_resource")) for c in causes}
    # "Nothing is wrong" names no resource; compare the category alone.
    if any(category == "no_action_needed" for category, _, _ in truth):
        reported = {
            (c, None, None) if c == "no_action_needed" else (c, k, n) for c, k, n in reported
        }

    matched = truth & reported
    return {
        "causes_expected": len(truth),
        "causes_matched": len(matched),
        "extra_causes": sorted(f"{c}:{n}" for c, _, n in reported - truth if c != "unknown"),
        "correct": bool(causes) and matched == truth and reported == truth,
        "answered_unknown": bool(causes) and all(c["category"] == "unknown" for c in causes),
        "reported": sorted(f"{c}:{n}" for c, _, n in reported),
    }
