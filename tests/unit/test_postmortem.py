from datetime import UTC, datetime
from pathlib import Path

import pytest

from heimdall.core.audit import AuditLog, EventType
from heimdall.core.models import IncidentState
from heimdall.core.store import connect
from heimdall.report.postmortem import build, render_html, render_markdown
from heimdall.report.store import PostmortemStore, PostmortemWriter
from tests.unit.test_pipeline import OPA, World, proposal, release_world, repo_with_bad_release

pytestmark = pytest.mark.skipif(not OPA.exists(), reason="run infra/tools.sh to download opa")


def report(world: World, diagnosis):
    return build(diagnosis.incident_id, world.audit.events(diagnosis.incident_id))


def test_dry_run_report_says_what_would_have_happened(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("rollback_deployment", to_revision=1),
                             proposal("open_pr", summary="revert the release commit")])  # fmt: skip
    diagnosis = world.diagnosed("bad_release", "frontend")
    world.pipeline.remediate(diagnosis)

    pm = report(world, diagnosis)

    assert pm.state is IncidentState.ESCALATED and pm.dry_run
    assert pm.services == ["frontend"]
    assert [a.alertname for a in pm.alerts] == ["HighErrorRate"]
    rollback, pr = pm.actions
    assert rollback.action == "rollback_deployment boutique/frontend to revision 1"
    assert rollback.decision == "allow" and rollback.status == "dry_run"
    assert "dry run" in rollback.admission
    assert pr.admission == "not sent to the API server"
    assert pm.drift.startswith("none")
    assert any("dry run" in item for item in pm.follow_ups)


def test_every_timeline_line_points_at_a_real_audit_event(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("scale_deployment", replicas=2)], live=True, recovers=[True])
    diagnosis = world.diagnosed("dependency_down", "frontend")
    world.pipeline.remediate(diagnosis)

    pm = report(world, diagnosis)

    assert pm.timeline
    for entry in pm.timeline:
        event = world.audit.get(entry.seq)
        assert event is not None and event.event_type == entry.event
        assert event.incident_id == diagnosis.incident_id
    assert [e.seq for e in pm.timeline] == sorted(e.seq for e in pm.timeline)
    assert pm.state is IncidentState.RESOLVED and pm.closed_at is not None
    assert pm.actions[0].verification == "recovered after 45s"
    assert pm.actions[0].admission == "accepted"


def test_a_fix_that_did_not_work_is_reported_as_undone(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("scale_deployment", replicas=3)], live=True,
                  recovers=[False])  # fmt: skip
    diagnosis = world.diagnosed("resource_saturation", "frontend")
    world.pipeline.remediate(diagnosis)

    pm = report(world, diagnosis)

    (action,) = pm.actions
    assert action.undone and action.status == "undone"
    assert action.verification is not None and action.verification.startswith("not recovered")
    assert pm.state is IncidentState.ESCALATED
    assert any("human has to take over" in item for item in pm.follow_ups)


def test_mitigated_incident_reports_drift_and_the_pull_request(tmp_path: Path) -> None:
    world, diagnosis = release_world(tmp_path, repo_with_bad_release(), live=True, recovers=[True])
    world.pipeline.remediate(diagnosis)

    pm = report(world, diagnosis)

    assert pm.state is IncidentState.MITIGATED and pm.mitigated_at is not None
    assert "paused" in pm.drift
    assert len(pm.pull_requests) == 1 and pm.pull_requests[0].startswith("http")
    assert any("merge" in item for item in pm.follow_ups)


def test_denied_action_shows_the_policy_reason(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("scale_deployment", replicas=0)])
    diagnosis = world.diagnosed("resource_saturation", "frontend")
    world.pipeline.remediate(diagnosis)

    pm = report(world, diagnosis)
    markdown = render_markdown(pm)

    denied = [a for a in pm.actions if a.decision == "deny"]
    assert denied or pm.dropped_proposals
    assert "deny" in markdown or "Dropped before the policy gate" in markdown


def _log_with(tmp_path: Path, summary: str, quote: str, label: str = "frontend"):
    audit = AuditLog(connect(tmp_path / "db.sqlite"))

    def add(kind: EventType, payload: dict) -> None:
        audit.append(kind, payload, actor="test", incident_id="inc_1")

    add(EventType.ALERT_RECEIVED, {"alertname": "HighErrorRate", "status": "firing",
        "fingerprint": "f1", "labels": {"service": label, "severity": "critical"}})  # fmt: skip
    add(EventType.STATE_TRANSITION, {"from": None, "to": "open", "reason": "alert"})
    add(EventType.TOOL_CALL, {"call_id": "tc_1", "tool": "list_pods", "arguments": {}})
    add(EventType.LLM_CALL, {"model": "m", "prompt_tokens": 100, "cached_tokens": 40,
        "completion_tokens": 20, "latency_seconds": 1.5, "cost_usd": 0.002,
        "reply": "{}"})  # fmt: skip
    cause = {"category": "bad_config", "summary": summary, "confidence": 0.9,
             "affected_resource": {"kind": "Deployment", "namespace": "boutique", "name": "x"},
             "evidence": [
                 {"tool_call_id": "tc_1", "quote": quote, "observation": "o"}]}  # fmt: skip
    add(EventType.DIAGNOSIS, {"diagnosis": {"incident_id": "inc_1", "root_causes": [cause],
        "ruled_out": [{"hypothesis": "node", "reason": "nodes ready", "tool_call_id": "tc_1"}]},
        "stopped_because": "diagnosed"})  # fmt: skip
    add(EventType.STATE_TRANSITION, {"from": "open", "to": "escalated", "reason": "r"})
    return audit


def test_agent_stats_are_summed_from_the_log(tmp_path: Path) -> None:
    pm = build("inc_1", _log_with(tmp_path, "s", "quote").events())

    assert (pm.stats.tool_calls, pm.stats.llm_calls) == (1, 1)
    assert (pm.stats.prompt_tokens, pm.stats.cached_tokens) == (100, 40)
    assert pm.stats.cost_usd == 0.002 and pm.stats.models == ["m"]
    assert pm.severity == "critical"
    assert pm.ruled_out[0].hypothesis == "node"


def test_unknown_incident_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        build("inc_nope", _log_with(tmp_path, "s", "quote").events())


def test_hostile_text_is_inert_in_both_renderings(tmp_path: Path) -> None:
    hostile = "<script>alert(1)</script> [click](javascript:x) `code` | cell"
    pm = build("inc_1", _log_with(tmp_path, hostile, "a ``` b <img src=x>", "<b>svc</b>").events())

    page = render_html(pm)
    assert "<script>" not in page and "<img" not in page and "<b>svc" not in page
    assert "&lt;script&gt;" in page

    markdown = render_markdown(pm)
    assert "<script>" not in markdown and "[click](" not in markdown
    assert "```` a ``` b <img src=x> ````" in markdown  # the quote stays verbatim, fenced


def test_secrets_in_the_log_do_not_reach_the_report(tmp_path: Path) -> None:
    key = "AKIAIOSFODNN7EXAMPLE"
    pm = build("inc_1", _log_with(tmp_path, f"leaked {key}", f"password=hunter2secret {key}",
                                  label=f"svc-{key}").events())  # fmt: skip

    for text in (render_markdown(pm), render_html(pm), pm.model_dump_json()):
        assert key not in text and "hunter2secret" not in text
        assert "[redacted:" in text


def test_report_is_written_when_the_incident_closes(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("scale_deployment", replicas=2)], live=True, recovers=[True])
    store = PostmortemStore(connect(tmp_path / "db.sqlite"))
    world.audit.subscribe(PostmortemWriter(world.audit, store).on_event)
    diagnosis = world.diagnosed("dependency_down", "frontend")
    assert store.get(diagnosis.incident_id) is None

    world.pipeline.remediate(diagnosis)

    stored = store.get(diagnosis.incident_id)
    assert stored is not None and stored.postmortem.state is IncidentState.RESOLVED
    assert stored.postmortem.opened_at <= datetime.now(UTC)


def test_a_broken_report_never_blocks_the_incident(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("scale_deployment", replicas=2)], live=True, recovers=[True])

    class Broken:
        def save(self, *_):
            raise RuntimeError("disk full")

    world.audit.subscribe(PostmortemWriter(world.audit, Broken()).on_event)  # type: ignore[arg-type]
    diagnosis = world.diagnosed("dependency_down", "frontend")
    world.pipeline.remediate(diagnosis)

    assert world.state(diagnosis) is IncidentState.RESOLVED


def test_cli_prints_the_report_and_sets_the_run_label(tmp_path: Path, capsys) -> None:
    from heimdall.cli import main

    _log_with(tmp_path, "config typo", "quote")
    db = str(tmp_path / "db.sqlite")

    assert main(["incident", "postmortem", "inc_1", "--db", db]) == 0
    assert "# Postmortem:" in capsys.readouterr().out
    out = tmp_path / "pm.html"
    assert main(["incident", "postmortem", "inc_1", "--db", db, "--html", "--out", str(out)]) == 0
    assert out.read_text().startswith("<!doctype html>")
    assert main(["incident", "postmortem", "inc_nope", "--db", db]) == 1

    capsys.readouterr()
    assert main(["label", "--db", db, "--scenario", "s04"]) == 0
    assert capsys.readouterr().out.strip() == "scenario=s04 set=dev"
    assert main(["label", "--db", db, "--clear"]) == 0
    assert capsys.readouterr().out.strip() == "scenario=- set=-"
