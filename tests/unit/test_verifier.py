from datetime import UTC, datetime

import httpx

from heimdall.core.models import ActionProposal, Incident, IncidentState
from heimdall.executor.verifier import Check, Verifier, recovery_checks

CHECKS = [Check("available", "avail >= 1"), Check("success", "rate >= 0.99")]


class Timeline:
    """Fake Prometheus whose answers depend on how many polls have happened,
    and a clock that only moves when the verifier sleeps."""

    def __init__(self, healthy_from: dict[str, int], broken_at: dict[str, set[int]] | None = None):
        self.healthy_from = healthy_from
        self.broken_at = broken_at or {}
        self.poll = 0
        self.now = 0.0
        self.queries: list[str] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params["query"]
            self.queries.append(query)
            ok = self.poll >= self.healthy_from.get(query, 10**9) and self.poll not in (
                self.broken_at.get(query, set())
            )
            result = [{"metric": {}, "value": [0, "1"]}] if ok else []
            return httpx.Response(200, json={"status": "success", "data": {"result": result}})

        return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://prom")

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.poll += 1

    def verifier(self, **kwargs) -> Verifier:
        return Verifier(self.client(), clock=lambda: self.now, sleep=self.sleep, **kwargs)


def test_recovered_once_every_check_holds_three_times_in_a_row() -> None:
    timeline = Timeline({"avail >= 1": 1, "rate >= 0.99": 4})

    result = timeline.verifier().verify(CHECKS)

    assert result.recovered
    # Healthy from poll 4; needs polls 4, 5 and 6; 15 s apart.
    assert result.seconds == 90
    assert result.failing == []


def test_a_blip_of_health_does_not_count() -> None:
    timeline = Timeline({"avail >= 1": 0, "rate >= 0.99": 0}, {"rate >= 0.99": {2}})

    result = timeline.verifier().verify(CHECKS)

    assert result.recovered
    assert result.seconds == 75  # polls 3, 4, 5 after the relapse at poll 2


def test_not_recovered_within_the_timeout() -> None:
    timeline = Timeline({"avail >= 1": 0})  # success rate never comes back

    result = timeline.verifier(timeout_seconds=300).verify(CHECKS)

    assert not result.recovered
    assert result.seconds == 300
    assert result.failing == ["success"]


def test_prometheus_being_down_is_not_recovery() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    now = [0.0]
    verifier = Verifier(
        httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://prom"),
        timeout_seconds=30,
        clock=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )

    assert not verifier.verify(CHECKS).recovered


def test_checks_cover_the_target_and_every_alerting_service() -> None:
    incident = Incident(
        id="inc_1", state=IncidentState.VERIFYING, namespace="boutique",
        services=["frontend", "cartservice"],
        opened_at=datetime(2026, 11, 10, tzinfo=UTC), updated_at=datetime(2026, 11, 10, tzinfo=UTC),
    )  # fmt: skip
    proposal = ActionProposal(type="scale_deployment", namespace="boutique", name="redis-cart",
                              replicas=1, justification="j", expected_effect="e")  # fmt: skip

    checks = recovery_checks(proposal, incident)

    assert [c.description for c in checks] == [
        "redis-cart has an available replica",
        "redis-cart has no unavailable replicas",
        "cartservice success rate is at least 99%",
        "frontend success rate is at least 99%",
        "redis-cart success rate is at least 99%",
    ]
    assert checks[0].promql == (
        'kube_deployment_status_replicas_available{namespace="boutique", deployment="redis-cart"}'
        " >= 1"
    )
