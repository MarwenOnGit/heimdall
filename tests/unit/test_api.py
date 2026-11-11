from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from heimdall.api import create_app
from heimdall.core.audit import AuditLog
from heimdall.core.confirm import PrometheusConfirmer, alerts_selector
from heimdall.core.incidents import IncidentManager
from heimdall.core.models import Alert
from heimdall.core.store import connect
from heimdall.tools.dependencies import DependencyGraph

TOKEN = "s3cret-token"  # noqa: S105
GRAPH = DependencyGraph({"frontend": ["cartservice"], "cartservice": []})


def payload(*alerts: dict) -> dict:
    return {"version": "4", "status": "firing", "receiver": "heimdall", "alerts": list(alerts)}


def firing(name: str = "HighErrorRate", service: str = "frontend") -> dict:
    return {
        "status": "firing",
        "labels": {"alertname": name, "namespace": "boutique", "service": service},
        "annotations": {"summary": "x"},
        "startsAt": "2026-11-10T12:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": f"{name}-{service}",
    }


class StaticConfirmer:
    def __init__(self, answer: bool) -> None:
        self.answer = answer

    def confirm(self, alert: Alert) -> bool:
        return self.answer


@pytest.fixture
def manager(tmp_path: Path) -> IncidentManager:
    conn = connect(tmp_path / "db.sqlite")
    clock = lambda: datetime(2026, 11, 10, 12, 0, tzinfo=UTC)  # noqa: E731
    return IncidentManager(conn, AuditLog(conn, clock), GRAPH, clock=clock)


def client(manager: IncidentManager, *, confirmed: bool = True) -> TestClient:
    app = create_app(alert_token=TOKEN, manager=manager, confirmer=StaticConfirmer(confirmed))
    return TestClient(app)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_authenticated_alert_opens_an_incident(manager: IncidentManager) -> None:
    response = client(manager).post("/alerts", json=payload(firing()), headers=AUTH)

    assert response.status_code == 202
    assert response.json() == {"accepted": 1}
    assert [i.services for i in manager.all()] == [["frontend"]]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Basic x"}],
)
def test_missing_or_wrong_token_is_rejected(manager: IncidentManager, headers: dict) -> None:
    response = client(manager).post("/alerts", json=payload(firing()), headers=headers)

    assert response.status_code == 401
    assert manager.all() == []


def test_forged_alert_with_valid_token_opens_nothing(manager: IncidentManager) -> None:
    """Test i07: Prometheus is not firing it, so it is ignored."""
    response = client(manager, confirmed=False).post(
        "/alerts", json=payload(firing()), headers=AUTH
    )

    assert response.status_code == 202
    assert manager.all() == []


def test_malformed_payload_is_rejected(manager: IncidentManager) -> None:
    response = client(manager).post("/alerts", json={"alerts": "nope"}, headers=AUTH)

    assert response.status_code == 422


def test_refuses_to_start_without_a_token(manager: IncidentManager) -> None:
    with pytest.raises(ValueError, match="token"):
        create_app(alert_token="", manager=manager, confirmer=StaticConfirmer(True))


def test_health_needs_no_token(manager: IncidentManager) -> None:
    assert client(manager).get("/healthz").status_code == 200


# ---------------------------------------------------------------- confirmer


def prometheus(result: list, *, status_code: int = 200) -> tuple[PrometheusConfirmer, list[str]]:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["query"])
        body = {"status": "success", "data": {"result": result}}
        return httpx.Response(status_code, json=body)

    transport = httpx.MockTransport(handler)
    return PrometheusConfirmer(httpx.Client(transport=transport, base_url="http://prom")), queries


def test_firing_alert_is_confirmed_when_prometheus_has_it() -> None:
    confirmer, queries = prometheus([{"metric": {}, "value": [0, "1"]}])
    alert = Alert.model_validate(firing())

    assert confirmer.confirm(alert) is True
    assert queries == [
        'ALERTS{alertname="HighErrorRate", namespace="boutique", service="frontend",'
        ' alertstate="firing"}'
    ]


def test_firing_alert_is_not_confirmed_when_prometheus_lacks_it() -> None:
    confirmer, _ = prometheus([])
    assert confirmer.confirm(Alert.model_validate(firing())) is False


def test_resolved_alert_is_confirmed_only_when_no_longer_firing() -> None:
    resolved = Alert.model_validate({**firing(), "status": "resolved"})

    assert prometheus([])[0].confirm(resolved) is True
    assert prometheus([{"metric": {}, "value": [0, "1"]}])[0].confirm(resolved) is False


def test_prometheus_failure_means_unconfirmed() -> None:
    confirmer, _ = prometheus([], status_code=500)
    assert confirmer.confirm(Alert.model_validate(firing())) is False


def test_label_values_cannot_break_out_of_the_selector() -> None:
    hostile = Alert.model_validate(
        {**firing(), "labels": {"alertname": 'x"} or vector(1) or ALERTS{a="', "b": "c\\"}}
    )
    assert alerts_selector(hostile) == (
        'ALERTS{alertname="x\\"} or vector(1) or ALERTS{a=\\"", b="c\\\\", alertstate="firing"}'
    )
