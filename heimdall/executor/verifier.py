"""Verification: did the action actually fix the problem?

Recovery is judged from metrics queried directly, not from alert state, which
lags by the alert's `for` duration and Alertmanager's grouping. A condition
must hold on several consecutive checks, so a brief blip of health during a
rollout does not count as recovered.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from heimdall.core.models import ActionProposal, Incident


@dataclass(frozen=True)
class Check:
    description: str
    # A PromQL expression that returns at least one sample when healthy.
    promql: str


@dataclass
class Verification:
    recovered: bool
    seconds: float
    # description -> whether it held at the last look
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def failing(self) -> list[str]:
        return [name for name, ok in self.checks.items() if not ok]


def recovery_checks(proposal: ActionProposal, incident: Incident) -> list[Check]:
    """What must be true for this action to count as having worked: its
    target is fully available, and every service that was alerting is
    answering requests again."""
    namespace = proposal.namespace or incident.namespace or ""
    checks = []
    if proposal.name:
        selector = f'namespace="{namespace}", deployment="{proposal.name}"'
        checks += [
            Check(
                f"{proposal.name} has an available replica",
                f"kube_deployment_status_replicas_available{{{selector}}} >= 1",
            ),
            Check(
                f"{proposal.name} has no unavailable replicas",
                f"kube_deployment_status_replicas_unavailable{{{selector}}} == 0",
            ),
        ]
    for service in sorted({*incident.services, *([proposal.name] if proposal.name else [])}):
        checks.append(
            Check(
                f"{service} success rate is at least 99%",
                "heimdall:service_success_rate:ratio_rate2m"
                f'{{namespace="{namespace}", service="{service}"}} >= 0.99',
            )
        )
    return checks


class Verifier:
    def __init__(
        self,
        prometheus: httpx.Client,
        *,
        timeout_seconds: float = 300,
        interval_seconds: float = 15,
        stable_checks: int = 3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._prometheus = prometheus
        self._timeout = timeout_seconds
        self._interval = interval_seconds
        self._stable = stable_checks
        self._clock = clock
        self._sleep = sleep

    def _holds(self, check: Check) -> bool:
        # Anything other than a clear "yes" from Prometheus is a "no".
        try:
            response = self._prometheus.get("/api/v1/query", params={"query": check.promql})
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return body.get("status") == "success" and len(body["data"]["result"]) > 0

    def verify(self, checks: list[Check]) -> Verification:
        """Poll until every check has held `stable_checks` times in a row, or
        the timeout passes."""
        started = self._clock()
        streak = 0
        latest: dict[str, bool] = {}
        while True:
            latest = {check.description: self._holds(check) for check in checks}
            streak = streak + 1 if all(latest.values()) else 0
            elapsed = self._clock() - started
            if streak >= self._stable:
                return Verification(True, round(elapsed, 1), latest)
            if elapsed >= self._timeout:
                return Verification(False, round(elapsed, 1), latest)
            self._sleep(self._interval)
