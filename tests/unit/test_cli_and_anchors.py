import base64
import json
from pathlib import Path

import httpx
import pytest

from heimdall import cli
from heimdall.core import anchor as anchoring
from heimdall.core.audit import AuditLog, EventType
from heimdall.core.store import connect


def fake_gitea() -> tuple[httpx.Client, dict]:
    """A stand-in for the Gitea contents API holding one file."""
    state: dict = {"content": None, "commits": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if state["content"] is None:
                return httpx.Response(404)
            encoded = base64.b64encode(state["content"].encode()).decode()
            return httpx.Response(
                200, json={"content": encoded, "sha": f"sha{len(state['commits'])}"}
            )
        body = json.loads(request.content)
        if request.method == "PUT" and body.get("sha") != f"sha{len(state['commits'])}":
            return httpx.Response(409)
        state["content"] = base64.b64decode(body["content"]).decode()
        state["commits"].append(body["message"])
        return httpx.Response(201, json={})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://gitea"), state


def test_gitea_publisher_appends_one_commit_per_anchor(tmp_path: Path) -> None:
    client, state = fake_gitea()
    publisher = anchoring.GiteaAnchorPublisher(client, "heimdall/audit-anchors")
    log = AuditLog(connect(tmp_path / "db.sqlite"))
    key = anchoring.generate_key()
    anchorer = anchoring.Anchorer(log, key, [publisher], every=100)

    log.append(EventType.TOOL_CALL, {"n": 1}, actor="t")
    anchorer.anchor_now()
    log.append(EventType.TOOL_CALL, {"n": 2}, actor="t")
    anchorer.anchor_now()

    assert state["commits"] == ["Anchor audit log at seq 1", "Anchor audit log at seq 3"]
    assert [a.seq for a in publisher.read()] == [1, 3]
    assert anchoring.verify(log, publisher.read(), key.public_key()) is None


def test_unreachable_publisher_is_recorded_not_fatal(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gitea is down")

    client = httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://gitea")
    anchor_file = tmp_path / "anchors.jsonl"
    log = AuditLog(connect(tmp_path / "db.sqlite"))
    anchorer = anchoring.Anchorer(
        log,
        anchoring.generate_key(),
        [anchoring.GiteaAnchorPublisher(client, "r"), anchoring.FileAnchorPublisher(anchor_file)],
    )
    log.append(EventType.TOOL_CALL, {"n": 1}, actor="t")

    anchorer.anchor_now()

    head = log.head()
    assert head is not None
    assert head.payload["failed"] == ["gitea"]
    assert head.payload["published"] == ["file"]
    assert len(anchoring.read_anchor_file(anchor_file)) == 1


@pytest.fixture
def audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    key = anchoring.generate_key()
    paths = {
        "db": tmp_path / "db.sqlite",
        "anchors": tmp_path / "anchors.jsonl",
        "key": tmp_path / "audit.key",
        "pub": tmp_path / "audit.pub",
    }
    paths["key"].write_bytes(anchoring.private_key_pem(key))
    paths["pub"].write_bytes(anchoring.public_key_pem(key.public_key()))
    log = AuditLog(connect(paths["db"]))
    anchorer = anchoring.Anchorer(log, key, [anchoring.FileAnchorPublisher(paths["anchors"])])
    for i in range(4):
        log.append(EventType.TOOL_CALL, {"i": i}, actor="t", incident_id="inc_1")
    anchorer.anchor_now()
    monkeypatch.delenv("HEIMDALL_AUDIT_KEY", raising=False)
    return paths


def verify(paths: dict[str, Path]) -> int:
    return cli.main(
        ["audit", "verify", "--db", str(paths["db"]), "--anchors", str(paths["anchors"]),
         "--public-key", str(paths["pub"])]
    )  # fmt: skip


def test_audit_verify_passes_on_an_intact_log(
    audited: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert verify(audited) == 0
    assert "OK: 5 events, chain and 1 anchors" in capsys.readouterr().out


def test_audit_verify_reports_the_first_broken_row(
    audited: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    conn = connect(audited["db"])
    conn.execute("UPDATE audit_log SET actor = 'someone-else' WHERE seq = 2")

    assert verify(audited) == 1
    assert "FAILED at seq 2" in capsys.readouterr().out


def test_audit_verify_without_anchors_checks_the_chain_only(
    audited: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["audit", "verify", "--db", str(audited["db"])]) == 0
    assert "chain only" in capsys.readouterr().out


def test_freeze_and_unfreeze_flip_the_flag_and_are_audited(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from heimdall.core.actions import ActionStore

    db = tmp_path / "db.sqlite"

    assert cli.main(["freeze", "--db", str(db)]) == 0
    assert ActionStore(connect(db)).frozen()
    assert cli.main(["unfreeze", "--db", str(db)]) == 0
    assert not ActionStore(connect(db)).frozen()

    events = [(e.event_type, e.payload["frozen"]) for e in AuditLog(connect(db)).events()]
    assert events == [("freeze", True), ("freeze", False)]
    assert "frozen" in capsys.readouterr().out
