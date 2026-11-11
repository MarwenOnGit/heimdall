import dataclasses
from pathlib import Path

import pytest

from heimdall.agent.setup import build_pipeline
from heimdall.config import Config
from heimdall.core.actions import ActionStore
from heimdall.core.audit import AuditLog
from heimdall.core.incidents import IncidentManager
from heimdall.core.store import connect
from heimdall.tools.dependencies import DependencyGraph


class NoLLM:
    model = "none"

    def complete(self, messages, response_schema=None):
        raise AssertionError("not expected to be called")


def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides):
    token = tmp_path / "token"
    token.write_text("reader-token\n")
    monkeypatch.setenv("HEIMDALL_KUBE_TOKEN_FILE", str(token))
    config = dataclasses.replace(Config.from_env(), kube_ca_file=None, **overrides)
    conn = connect(tmp_path / "db.sqlite")
    audit = AuditLog(conn)
    manager = IncidentManager(conn, audit, DependencyGraph({}))
    return build_pipeline(config, audit, NoLLM(), manager, ActionStore(conn))


def test_observe_mode_has_no_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert build(tmp_path, monkeypatch, mode="observe") is None


def test_unknown_mode_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        build(tmp_path, monkeypatch, mode="yolo")


def test_live_mode_also_needs_the_actor_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="HEIMDALL_ACTOR_TOKEN_FILE"):
        build(tmp_path, monkeypatch, mode="live", actor_token_file=None)

    actor = tmp_path / "actor-token"
    actor.write_text("actor-token\n")
    pipeline = build(tmp_path, monkeypatch, mode="live", actor_token_file=actor)
    assert pipeline is not None and pipeline._live  # noqa: SLF001


def test_dry_run_needs_a_separate_actor_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="HEIMDALL_ACTOR_TOKEN_FILE"):
        build(tmp_path, monkeypatch, mode="dry_run", actor_token_file=None)

    actor = tmp_path / "actor-token"
    actor.write_text("actor-token\n")
    assert build(tmp_path, monkeypatch, mode="dry_run", actor_token_file=actor) is not None
