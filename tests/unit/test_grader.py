from chaos.grader import grade

FRONTEND = {"kind": "Deployment", "namespace": "boutique", "name": "frontend"}
REDIS = {"kind": "Deployment", "namespace": "boutique", "name": "redis-cart"}
TRUTH = [{"category": "dependency_down", "resource": REDIS}]


def diagnosis(*causes: tuple[str, dict | None]) -> dict:
    return {"root_causes": [{"category": c, "affected_resource": r} for c, r in causes]}


def test_right_category_and_resource_is_correct() -> None:
    result = grade(TRUTH, diagnosis(("dependency_down", REDIS)))
    assert result["correct"] and result["causes_matched"] == 1


def test_blaming_the_symptom_is_wrong() -> None:
    result = grade(TRUTH, diagnosis(("bad_release", FRONTEND)))
    assert not result["correct"]
    assert result["extra_causes"] == ["bad_release:frontend"]


def test_right_resource_wrong_category_is_wrong() -> None:
    assert not grade(TRUTH, diagnosis(("bad_config", REDIS)))["correct"]


def test_right_category_wrong_resource_is_wrong() -> None:
    assert not grade(TRUTH, diagnosis(("dependency_down", FRONTEND)))["correct"]


def test_extra_blame_makes_it_wrong() -> None:
    result = grade(TRUTH, diagnosis(("dependency_down", REDIS), ("bad_release", FRONTEND)))
    assert not result["correct"] and result["causes_matched"] == 1


def test_two_faults_need_both() -> None:
    truth = [*TRUTH, {"category": "bad_release", "resource": FRONTEND}]
    partial = grade(truth, diagnosis(("dependency_down", REDIS)))
    full = grade(truth, diagnosis(("bad_release", FRONTEND), ("dependency_down", REDIS)))

    assert (partial["correct"], partial["causes_matched"]) == (False, 1)
    assert full["correct"]


def test_unknown_is_not_correct_but_is_flagged_as_safe() -> None:
    result = grade(TRUTH, diagnosis(("unknown", None)))
    assert not result["correct"] and result["answered_unknown"]
    assert result["extra_causes"] == []


def test_no_diagnosis_at_all_is_wrong() -> None:
    assert not grade(TRUTH, None)["correct"]


def test_nothing_wrong_matches_whatever_resource_is_named() -> None:
    truth = [{"category": "no_action_needed", "resource": None}]
    assert grade(truth, diagnosis(("no_action_needed", FRONTEND)))["correct"]
    assert grade(truth, diagnosis(("no_action_needed", None)))["correct"]
    assert not grade(truth, diagnosis(("bad_release", FRONTEND)))["correct"]
