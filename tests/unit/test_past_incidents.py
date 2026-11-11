from datetime import UTC, datetime
from pathlib import Path

from heimdall.agent.validator import validate
from heimdall.core.models import Diagnosis
from heimdall.core.store import connect
from heimdall.report.postmortem import Postmortem
from heimdall.report.store import PostmortemStore, RunLabel, bm25, search
from heimdall.tools.history import NOT_EVIDENCE, HistoryTools, SearchArgs


def postmortem(incident_id: str, service: str, category: str, summary: str) -> Postmortem:
    resource = {"kind": "Deployment", "namespace": "boutique", "name": service}
    return Postmortem.model_validate(
        {
            "incident_id": incident_id,
            "state": "resolved",
            "severity": "critical",
            "services": [service],
            "alerts": [{"seq": 1, "time": datetime.now(UTC), "alertname": "HighErrorRate",
                        "service": service}],
            "opened_at": datetime.now(UTC),
            "root_causes": [{"category": category, "summary": summary, "confidence": 0.9,
                             "affected_resource": resource}],
            "last_seq": 9,
        }
    )  # fmt: skip


def store_with(tmp_path: Path) -> PostmortemStore:
    store = PostmortemStore(connect(tmp_path / "db.sqlite"))
    store.save(postmortem("inc_a", "frontend", "bad_release", "revision 7 returns errors"),
               RunLabel("s04", "dev"))  # fmt: skip
    store.save(postmortem("inc_b", "cartservice", "dependency_down", "redis-cart scaled to zero"),
               RunLabel("s07", "dev"))  # fmt: skip
    store.save(postmortem("inc_h", "frontend", "bad_release", "secret hold-out fault"),
               RunLabel("h01", "holdout"))  # fmt: skip
    store.save(postmortem("inc_p", "adservice", "resource_limits_too_low", "OOMKilled at 64Mi"))
    return store


def ids(found) -> list[str]:
    return [s.postmortem.incident_id for s in found]


def test_bm25_prefers_the_document_that_matches_rare_terms() -> None:
    scores = bm25("redis scaled to zero", ["frontend errors after release", "redis scaled zero",
                                           "frontend errors again"])  # fmt: skip
    assert scores[1] > 0 and scores[0] == scores[2] == 0
    assert bm25("anything", []) == []


def test_search_finds_the_similar_incident(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    assert ids(search(store, "cartservice cannot reach redis"))[0] == "inc_b"
    assert search(store, "zzz nothing matches") == []


def test_holdout_reports_are_never_searchable(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    for label in (RunLabel(), RunLabel("s01", "dev"), RunLabel("h02", "holdout")):
        store.set_label(label)
        assert "inc_h" not in ids(store.searchable())
        assert "inc_h" not in ids(search(store, "frontend bad release hold-out fault"))


def test_a_run_never_sees_reports_about_its_own_scenario(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    store.set_label(RunLabel("s04", "dev"))
    # Not its own scenario, not hold-out, and not the unlabelled one: nothing
    # says that one is not about s04 too.
    assert ids(store.searchable()) == ["inc_b"]


def test_holdout_runs_search_dev_reports_only(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    store.set_label(RunLabel("h01", "holdout"))
    assert sorted(ids(store.searchable())) == ["inc_a", "inc_b"]


def test_outside_a_benchmark_everything_but_holdout_is_searchable(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    store.set_label(RunLabel("s04", "dev"))
    store.set_label(RunLabel())
    assert sorted(ids(store.searchable())) == ["inc_a", "inc_b", "inc_p"]


def test_resaving_keeps_the_label_the_incident_ran_under(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    store.set_label(RunLabel())  # the run is over when the pull request is merged
    store.save(postmortem("inc_a", "frontend", "bad_release", "updated"))
    stored = store.get("inc_a")
    assert stored is not None and stored.label == RunLabel("s04", "dev")
    assert stored.postmortem.root_causes[0].summary == "updated"


def test_tool_output_names_cause_and_says_it_is_not_evidence(tmp_path: Path) -> None:
    tools = HistoryTools(store_with(tmp_path))
    text = tools.search_past_incidents(SearchArgs(query="frontend errors after release"))
    assert "not evidence" in text
    assert "incident=inc_a" in text and "root_cause=bad_release resource=frontend" in text
    assert tools.search_past_incidents(SearchArgs(query="qqq")) == "no similar past incidents"


def test_a_past_incident_cannot_be_cited_as_evidence() -> None:
    resource = {"kind": "Deployment", "namespace": "boutique", "name": "frontend"}
    diagnosis = Diagnosis.model_validate({"incident_id": "inc_1", "root_causes": [{
        "category": "bad_release", "summary": "s", "confidence": 0.9,
        "affected_resource": resource,
        "evidence": [{"tool_call_id": "tc_1", "quote": "restarts=9", "observation": "o"},
                     {"tool_call_id": "tc_2", "quote": "root_cause=bad_release",
                      "observation": "same as last time"}]}]})  # fmt: skip
    outputs = {"tc_1": "frontend restarts=9", "tc_2": "incident=inc_a root_cause=bad_release"}

    assert validate(diagnosis, outputs) == []
    problems = validate(diagnosis, outputs, not_evidence={"tc_2"})
    assert any("cannot be evidence" in p for p in problems)
    assert any("at least 2 different tool calls" in p for p in problems)
    assert "search_past_incidents" in NOT_EVIDENCE
