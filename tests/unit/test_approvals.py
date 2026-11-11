from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from heimdall.core.approvals import ApprovalError, ApprovalStatus, ApprovalStore
from heimdall.core.models import ActionProposal
from heimdall.core.store import connect
from heimdall.notify.webhook import WebhookNotifier

SECRET = b"0123456789abcdef0123456789abcdef"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def scale(replicas: int = 1) -> ActionProposal:
    return ActionProposal(type="scale_deployment", namespace="boutique", name="redis-cart",
                          replicas=replicas, justification="j", expected_effect="e")  # fmt: skip


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> ApprovalStore:
    return ApprovalStore(connect(tmp_path / "db.sqlite"), SECRET, clock=clock)


def test_approval_with_the_right_token_is_recorded(store: ApprovalStore) -> None:
    approval = store.request("inc_1", scale(), "100")

    decided = store.decide(approval.id, store.token(approval), approve=True)

    assert decided.status is ApprovalStatus.APPROVED
    assert decided.proposal == scale()
    assert decided.resource_version == "100"


def test_rejection_is_recorded(store: ApprovalStore) -> None:
    approval = store.request("inc_1", scale(), "100")
    assert store.decide(approval.id, store.token(approval), approve=False).status is (
        ApprovalStatus.REJECTED
    )


@pytest.mark.parametrize("token", ["", "0" * 64, "not-a-token"])
def test_wrong_token_is_refused_and_changes_nothing(store: ApprovalStore, token: str) -> None:
    approval = store.request("inc_1", scale(), "100")

    with pytest.raises(ApprovalError, match="invalid approval id or token"):
        store.decide(approval.id, token, approve=True)

    current = store.get(approval.id)
    assert current is not None and current.status is ApprovalStatus.PENDING


def test_unknown_id_looks_the_same_as_a_wrong_token(store: ApprovalStore) -> None:
    with pytest.raises(ApprovalError, match="invalid approval id or token"):
        store.decide("deadbeefdeadbeef", "0" * 64, approve=True)


def test_token_cannot_approve_a_different_action(store: ApprovalStore) -> None:
    """Approving 'scale to 1' must not authorize 'scale to 5'."""
    small = store.request("inc_1", scale(1), "100")
    big = store.request("inc_1", scale(5), "100")

    with pytest.raises(ApprovalError):
        store.decide(big.id, store.token(small), approve=True)


def test_token_is_bound_to_the_version_of_the_target(store: ApprovalStore) -> None:
    a = store.request("inc_1", scale(), "100")
    b = store.request("inc_1", scale(), "101")

    assert store.token(a) != store.token(b)


def test_token_from_another_secret_is_refused(tmp_path: Path, store: ApprovalStore) -> None:
    approval = store.request("inc_1", scale(), "100")
    other = ApprovalStore(connect(tmp_path / "db.sqlite"), b"another-secret-of-enough-length!")

    with pytest.raises(ApprovalError):
        store.decide(approval.id, other.token(approval), approve=True)


def test_approval_works_only_once(store: ApprovalStore) -> None:
    approval = store.request("inc_1", scale(), "100")
    store.decide(approval.id, store.token(approval), approve=True)

    with pytest.raises(ApprovalError, match="already approved"):
        store.decide(approval.id, store.token(approval), approve=True)


def test_expired_approval_is_refused(store: ApprovalStore, clock: Clock) -> None:
    approval = store.request("inc_1", scale(), "100")
    clock.now += timedelta(minutes=15)

    with pytest.raises(ApprovalError, match="expired"):
        store.decide(approval.id, store.token(approval), approve=True)

    current = store.get(approval.id)
    assert current is not None and current.status is ApprovalStatus.EXPIRED


def test_expire_due_returns_only_overdue_pending_approvals(
    store: ApprovalStore, clock: Clock
) -> None:
    old = store.request("inc_1", scale(1), "100")
    decided = store.request("inc_2", scale(2), "100")
    store.decide(decided.id, store.token(decided), approve=False)
    clock.now += timedelta(minutes=10)
    fresh = store.request("inc_3", scale(3), "100")
    clock.now += timedelta(minutes=6)

    expired = store.expire_due()

    assert [a.id for a in expired] == [old.id]
    assert store.expire_due() == []
    current = store.get(fresh.id)
    assert current is not None and current.status is ApprovalStatus.PENDING


def test_short_secret_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 16 bytes"):
        ApprovalStore(connect(tmp_path / "db.sqlite"), b"short")


def test_webhook_notifier_posts_and_survives_an_outage() -> None:
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    WebhookNotifier(client, "http://chat/hook").send("Approval needed", "scale redis-cart to 1")

    assert sent[0]["content"] == "**Approval needed**\nscale redis-cart to 1"
    assert sent[0]["text"] == sent[0]["content"]

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    down = httpx.Client(transport=httpx.MockTransport(refuse))
    WebhookNotifier(down, "http://chat/hook").send("t", "x")  # must not raise
