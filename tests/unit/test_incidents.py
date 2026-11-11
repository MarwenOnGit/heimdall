from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from heimdall.core.audit import AuditLog
from heimdall.core.incidents import IncidentManager, InvalidTransition
from heimdall.core.models import Alert, IncidentState
from heimdall.core.store import connect
from heimdall.tools.dependencies import DependencyGraph

GRAPH_FILE = Path(__file__).parents[2] / "docs" / "dependencies.yaml"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "heimdall.sqlite"


def make_manager(db_path: Path, clock: Clock) -> tuple[IncidentManager, AuditLog]:
    conn = connect(db_path)
    audit = AuditLog(conn, clock)
    return IncidentManager(conn, audit, DependencyGraph.load(GRAPH_FILE), clock=clock), audit


@pytest.fixture
def manager(db_path: Path, clock: Clock) -> IncidentManager:
    return make_manager(db_path, clock)[0]


def alert(
    name: str, service: str | None, *, status: str = "firing", namespace: str | None = "boutique"
) -> Alert:
    labels = {"alertname": name, "heimdall": "true"}
    if service:
        labels["service"] = service
    if namespace and service:
        labels["namespace"] = namespace
    return Alert.model_validate(
        {
            "status": status,
            "labels": labels,
            "startsAt": "2026-11-10T12:00:00Z",
            "fingerprint": f"{name}-{service}",
        }
    )


def test_first_alert_opens_an_incident(manager: IncidentManager) -> None:
    incident = manager.ingest(alert("HighErrorRate", "frontend"), confirmed=True)

    assert incident is not None
    assert incident.id == "inc_2026_11_10_0001"
    assert incident.state is IncidentState.OPEN
    assert incident.services == ["frontend"]


def test_unconfirmed_alert_never_opens_an_incident(db_path: Path, clock: Clock) -> None:
    manager, audit = make_manager(db_path, clock)

    assert manager.ingest(alert("HighErrorRate", "frontend"), confirmed=False) is None
    assert manager.all() == []
    assert [e.event_type for e in audit.events()] == ["alert_unconfirmed"]


def test_same_service_joins_the_open_incident(manager: IncidentManager, clock: Clock) -> None:
    first = manager.ingest(alert("PodCrashLooping", "cartservice"), confirmed=True)
    clock.advance(minutes=2)
    second = manager.ingest(alert("PodNotReady", "cartservice"), confirmed=True)

    assert first is not None and second is not None
    assert second.id == first.id
    assert len(manager.all()) == 1


def test_redis_outage_is_one_incident_not_two(manager: IncidentManager, clock: Clock) -> None:
    """Scenario s07: frontend errors and cartservice latency share a cause."""
    a = manager.ingest(alert("HighErrorRate", "frontend"), confirmed=True)
    clock.advance(seconds=40)
    b = manager.ingest(alert("HighLatencyP95", "cartservice"), confirmed=True)
    clock.advance(seconds=40)
    c = manager.ingest(alert("DeploymentReplicasMismatch", "redis-cart"), confirmed=True)

    assert a is not None and b is not None and c is not None
    assert a.id == b.id == c.id
    # redis-cart is not a neighbour of frontend; it joined through cartservice.
    assert c.services == ["frontend", "cartservice", "redis-cart"]


def test_unrelated_services_get_separate_incidents(manager: IncidentManager) -> None:
    a = manager.ingest(alert("ContainerOOMKilled", "adservice"), confirmed=True)
    b = manager.ingest(alert("ImagePullFailing", "paymentservice"), confirmed=True)

    assert a is not None and b is not None
    assert a.id != b.id


def test_alert_after_the_window_opens_a_new_incident(
    manager: IncidentManager, clock: Clock
) -> None:
    a = manager.ingest(alert("PodCrashLooping", "cartservice"), confirmed=True)
    clock.advance(minutes=6)
    b = manager.ingest(alert("PodNotReady", "cartservice"), confirmed=True)

    assert a is not None and b is not None
    assert a.id != b.id


def test_window_slides_with_each_new_alert(manager: IncidentManager, clock: Clock) -> None:
    a = manager.ingest(alert("PodCrashLooping", "cartservice"), confirmed=True)
    clock.advance(minutes=4)
    manager.ingest(alert("PodNotReady", "cartservice"), confirmed=True)
    clock.advance(minutes=4)
    c = manager.ingest(alert("HighErrorRate", "cartservice"), confirmed=True)

    assert a is not None and c is not None
    assert a.id == c.id


def test_node_alert_joins_the_workload_incident(manager: IncidentManager, clock: Clock) -> None:
    a = manager.ingest(alert("HighErrorRate", "frontend"), confirmed=True)
    clock.advance(seconds=30)
    b = manager.ingest(alert("NodeNotReady", None), confirmed=True)

    assert a is not None and b is not None
    assert a.id == b.id


def test_resolved_alert_attaches_without_opening(manager: IncidentManager, clock: Clock) -> None:
    opened = manager.ingest(alert("HighErrorRate", "frontend"), confirmed=True)
    clock.advance(minutes=10)
    resolved = manager.ingest(alert("HighErrorRate", "frontend", status="resolved"), confirmed=True)
    stray = manager.ingest(alert("PodNotReady", "adservice", status="resolved"), confirmed=True)

    assert opened is not None and resolved is not None
    assert resolved.id == opened.id
    assert stray is None
    assert len(manager.all()) == 1


def test_flapping_alert_is_marked_and_stays_in_one_incident(
    manager: IncidentManager, clock: Clock
) -> None:
    incident = None
    for _ in range(3):
        incident = manager.ingest(alert("LatencyBudgetBurn", "paymentservice"), confirmed=True)
        clock.advance(seconds=50)
        incident = manager.ingest(
            alert("LatencyBudgetBurn", "paymentservice", status="resolved"), confirmed=True
        )
        clock.advance(seconds=70)

    assert incident is not None
    assert incident.flapping
    assert len(manager.all()) == 1


def test_only_allowed_transitions_are_accepted(manager: IncidentManager) -> None:
    incident = manager.ingest(alert("HighErrorRate", "frontend"), confirmed=True)
    assert incident is not None

    with pytest.raises(InvalidTransition):
        manager.transition(incident.id, IncidentState.EXECUTING, reason="skip ahead")
    manager.transition(incident.id, IncidentState.INVESTIGATING, reason="start")
    manager.transition(incident.id, IncidentState.ESCALATED, reason="budget exhausted")
    with pytest.raises(InvalidTransition):
        manager.transition(incident.id, IncidentState.OPEN, reason="reopen")


def test_every_transition_is_audited(db_path: Path, clock: Clock) -> None:
    manager, audit = make_manager(db_path, clock)
    incident = manager.ingest(alert("HighErrorRate", "frontend"), confirmed=True)
    assert incident is not None
    manager.transition(incident.id, IncidentState.INVESTIGATING, reason="start")

    moves = [
        (e.payload["from"], e.payload["to"])
        for e in audit.events(incident.id)
        if e.event_type == "state_transition"
    ]
    assert moves == [(None, "open"), ("open", "investigating")]
    assert audit.verify_chain() is None


def test_state_survives_a_restart_and_interrupted_work_is_requeued(
    db_path: Path, clock: Clock
) -> None:
    manager, _ = make_manager(db_path, clock)
    investigating = manager.ingest(alert("HighErrorRate", "frontend"), confirmed=True)
    done = manager.ingest(alert("ImagePullFailing", "paymentservice"), confirmed=True)
    assert investigating is not None and done is not None
    manager.transition(investigating.id, IncidentState.INVESTIGATING, reason="start")
    manager.transition(done.id, IncidentState.ESCALATED, reason="test")

    restarted, audit = make_manager(db_path, clock)
    pending = restarted.reconcile()

    assert [(i.id, i.state) for i in pending] == [(investigating.id, IncidentState.OPEN)]
    assert restarted.get(done.id).state is IncidentState.ESCALATED
    assert audit.verify_chain() is None


def test_dependency_graph_matches_the_workload() -> None:
    graph = DependencyGraph.load(GRAPH_FILE)

    assert graph.upstream("cartservice") == {"redis-cart"}
    assert graph.downstream("redis-cart") == {"cartservice"}
    assert graph.connected("frontend", "cartservice")
    assert not graph.connected("frontend", "redis-cart")
    assert len(graph.services) == 12
