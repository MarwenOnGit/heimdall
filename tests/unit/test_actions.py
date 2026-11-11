from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from heimdall.core.actions import ActionStatus, ActionStore, idempotency_key
from heimdall.core.models import ActionProposal
from heimdall.core.store import connect


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def proposal(kind: str = "restart_deployment", name: str = "frontend", **params) -> ActionProposal:
    return ActionProposal(type=kind, namespace="boutique", name=name, justification="because",
                          expected_effect="healthy", **params)  # fmt: skip


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> ActionStore:
    return ActionStore(connect(tmp_path / "db.sqlite"), clock)


def ran(store: ActionStore, incident: str, p: ActionProposal, status=ActionStatus.DRY_RUN) -> None:
    store.record(incident, p, decision="allow", status=status, detail="ok")


def test_key_depends_on_what_is_done_not_on_the_wording() -> None:
    a = proposal("scale_deployment", replicas=3)
    reworded = a.model_copy(update={"justification": "other words", "expected_effect": "x"})

    assert idempotency_key("inc_1", a) == idempotency_key("inc_1", reworded)
    assert idempotency_key("inc_1", a) != idempotency_key("inc_2", a)
    assert idempotency_key("inc_1", a) != idempotency_key(
        "inc_1", proposal("scale_deployment", replicas=4)
    )


def test_recording_the_same_action_twice_keeps_one_row(store: ActionStore) -> None:
    p = proposal()
    store.record("inc_1", p, decision="allow", status=ActionStatus.FAILED, detail="timeout")
    store.record("inc_1", p, decision="allow", status=ActionStatus.DRY_RUN, detail="ok")

    (only,) = store.for_incident("inc_1")
    assert only.status is ActionStatus.DRY_RUN
    assert store.find("inc_1", p) == only
    assert store.find("inc_2", p) is None


def test_only_cluster_writes_that_ran_count(store: ActionStore) -> None:
    ran(store, "inc_1", proposal("restart_deployment"))
    ran(store, "inc_1", proposal("scale_deployment", replicas=3), ActionStatus.EXECUTED)
    ran(store, "inc_1", proposal("patch_resources", limits={"memory": "256Mi"}),
        ActionStatus.DENIED)  # fmt: skip
    ran(store, "inc_1", proposal("rollback_deployment", to_revision=2),
        ActionStatus.REJECTED_BY_API)  # fmt: skip
    ran(store, "inc_1", proposal("escalate", name="frontend"), ActionStatus.RECORDED)
    ran(store, "inc_2", proposal("restart_deployment"))

    assert store.writes_so_far("inc_1") == 2
    assert store.writes_so_far("inc_2") == 1


def test_recent_actions_span_incidents_and_age_out(store: ActionStore, clock: Clock) -> None:
    ran(store, "inc_1", proposal("restart_deployment"))
    clock.now += timedelta(minutes=4)
    ran(store, "inc_2", proposal("scale_deployment", name="adservice", replicas=2))
    clock.now += timedelta(minutes=3)

    recent = store.recent()

    assert [(r["type"], r["name"], r["minutes_ago"], r["incident_id"]) for r in recent] == [
        ("restart_deployment", "frontend", 7, "inc_1"),
        ("scale_deployment", "adservice", 3, "inc_2"),
    ]
    clock.now += timedelta(minutes=30)
    assert store.recent() == []


def test_freeze_flag_persists(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = ActionStore(connect(path))
    assert not store.frozen()

    store.set_frozen(True)

    assert ActionStore(connect(path)).frozen()
    store.set_frozen(False)
    assert not ActionStore(connect(path)).frozen()
