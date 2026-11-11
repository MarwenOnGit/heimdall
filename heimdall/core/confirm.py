"""Alert confirmation: is Prometheus really firing this alert?

The webhook is authenticated, but a token can leak. Checking every alert
against Prometheus means a forged payload cannot invent an incident: it can
only refer to alerts that Prometheus itself is firing.
"""

from typing import Protocol

import httpx

from heimdall.core.models import Alert


class AlertConfirmer(Protocol):
    def confirm(self, alert: Alert) -> bool:
        """True when Prometheus agrees with the alert's claimed status."""
        ...


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def alerts_selector(alert: Alert) -> str:
    """PromQL selector matching exactly this alert in the ALERTS series."""
    matchers = [f"{name}={_quote(value)}" for name, value in sorted(alert.labels.items())]
    matchers.append('alertstate="firing"')
    return "ALERTS{" + ", ".join(matchers) + "}"


class PrometheusConfirmer:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def confirm(self, alert: Alert) -> bool:
        # Fail closed: if Prometheus cannot be asked, the alert is unconfirmed.
        try:
            response = self._client.get("/api/v1/query", params={"query": alerts_selector(alert)})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        if body.get("status") != "success":
            return False
        firing = len(body["data"]["result"]) > 0
        return firing if alert.status == "firing" else not firing
