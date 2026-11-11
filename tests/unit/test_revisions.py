from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from heimdall.core.actions import ActionStore
from heimdall.core.models import ActionProposal, Diagnosis
from heimdall.core.revisions import RevisionTracker, is_available
from heimdall.core.store import connect
from heimdall.policy.gate import OpaEvalGate
from heimdall.policy.input_builder import InputBuilder
from heimdall.tools.kube import KubeClient
from tests.unit.fake_kube import FakeKube
from tests.unit.test_pipeline import OPA

START = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)


def deployment(revision: int, *, healthy: bool = True, replicas: int = 1) -> dict:
    ready = replicas if healthy else 0
    return {
        "metadata": {"name": "frontend", "generation": 4,
                     "annotations": {"deployment.kubernetes.io/revision": str(revision)}},
        "spec": {"replicas": replicas},
        "status": {"observedGeneration": 4, "updatedReplicas": replicas,
                   "availableReplicas": ready,
                   "conditions": [{"type": "Available",
                                   "status": "True" if healthy else "False"}]},
    }  # fmt: skip


class Cluster:
    """A Deployment whose state the test changes between looks."""

    def __init__(self, tmp_path: Path) -> None:
        self.now = START
        self.state = deployment(1)
        self.conn = connect(tmp_path / "db.sqlite")
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": [self.state]})
        )
        kube = KubeClient(httpx.Client(transport=transport, base_url="https://kube"))
        self.tracker = RevisionTracker(kube, self.conn, {"boutique"}, clock=lambda: self.now)

    def look(self, after_seconds: float = 30, times: int = 1) -> None:
        for _ in range(times):
            self.now += timedelta(seconds=after_seconds)
            self.tracker.observe()

    def minutes(self) -> dict[int, float]:
        return self.tracker.available_minutes("boutique", "frontend")


def test_availability_needs_a_complete_healthy_rollout() -> None:
    assert is_available(deployment(1))
    assert not is_available(deployment(1, healthy=False))
    assert not is_available(deployment(1, replicas=0))
    rolling = deployment(1, replicas=3)
    rolling["status"]["updatedReplicas"] = 1
    assert not is_available(rolling)
    stale = deployment(1)
    stale["status"]["observedGeneration"] = 3
    assert not is_available(stale)


def test_a_healthy_revision_is_credited_with_the_time_it_served(tmp_path: Path) -> None:
    cluster = Cluster(tmp_path)
    cluster.look(times=25)  # the first look starts the clock; 24 intervals follow
    assert cluster.minutes() == {1: 12.0}


def test_a_revision_that_never_became_healthy_gets_nothing(tmp_path: Path) -> None:
    cluster = Cluster(tmp_path)
    cluster.look(times=5)
    cluster.state = deployment(2, healthy=False)  # the bad release
    cluster.look(times=40)
    assert cluster.minutes() == {1: 2.0, 2: 0.0}


def test_time_spent_broken_is_not_credited_on_recovery(tmp_path: Path) -> None:
    cluster = Cluster(tmp_path)
    cluster.look(times=3)
    cluster.state = deployment(1, healthy=False)
    cluster.look(times=20)
    cluster.state = deployment(1)
    cluster.look(times=3)
    assert cluster.minutes() == {1: 2.0}  # 1 minute before, 1 minute after


def test_time_heimdall_was_not_watching_is_not_credited(tmp_path: Path) -> None:
    cluster = Cluster(tmp_path)
    cluster.look(times=3)
    cluster.look(after_seconds=6 * 3600)  # Heimdall was down
    cluster.look(times=2)
    assert cluster.minutes() == {1: 2.0}


# ---------------------------------------------------- effect on the policy

needs_opa = pytest.mark.skipif(not OPA.exists(), reason="run infra/tools.sh to download opa")


def rollback_decision(tmp_path: Path, credit: dict[int, float] | None) -> tuple[list, str]:
    """Policy decision for rolling frontend back to revision 1. In the fake
    cluster revision 1 was current for 28 hours, which the ReplicaSet estimate
    counts as proven."""
    kube = FakeKube()
    kube.add("frontend")
    conn = connect(tmp_path / "db.sqlite")
    tracker = RevisionTracker(kube.client(), conn, {"boutique"})
    for revision, minutes in (credit or {}).items():
        conn.execute(
            "INSERT INTO revision_availability VALUES ('boutique', 'frontend', ?, ?, ?, NULL)",
            (revision, START.isoformat(), minutes * 60),
        )
    builder = InputBuilder(kube.client(), ActionStore(conn), mode="live", revisions=tracker)
    resource = {"kind": "Deployment", "namespace": "boutique", "name": "frontend"}
    diagnosis = Diagnosis.model_validate({"incident_id": "inc_1", "root_causes": [
        {"category": "bad_release", "summary": "s", "confidence": 0.9,
         "affected_resource": resource}]})  # fmt: skip
    proposal = ActionProposal(type="rollback_deployment", namespace="boutique", name="frontend",
                              to_revision=1, justification="j", expected_effect="e")  # fmt: skip
    built = builder.build(proposal, diagnosis)
    return built.data["revision_history"], OpaEvalGate(OPA).decide(built.data).decision


@needs_opa
def test_without_measurements_the_replicaset_estimate_is_used(tmp_path: Path) -> None:
    history, decision = rollback_decision(tmp_path, None)
    assert history[0]["revision"] == 1 and history[0]["available_minutes"] > 600
    assert decision == "allow"


@needs_opa
def test_a_measured_healthy_revision_is_a_rollback_target(tmp_path: Path) -> None:
    history, decision = rollback_decision(tmp_path, {1: 45.0, 2: 0.0})
    assert history == [{"revision": 1, "available_minutes": 45.0}]
    assert decision == "allow"


@needs_opa
def test_a_revision_measured_as_never_healthy_is_refused(tmp_path: Path) -> None:
    """The case the estimate got wrong: revision 1 was current for a day but
    was broken the whole time."""
    history, decision = rollback_decision(tmp_path, {1: 0.0, 2: 0.0})
    assert history == [{"revision": 1, "available_minutes": 0.0}]
    assert decision == "deny"
