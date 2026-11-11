"""HTTP API: alert ingest, human approvals, and Heimdall's own metrics."""

import hmac
import threading
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Response, status
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)
from pydantic import BaseModel

from heimdall.core.approvals import Approval, ApprovalError, ApprovalStatus, ApprovalStore
from heimdall.core.confirm import AlertConfirmer
from heimdall.core.incidents import IncidentManager
from heimdall.core.models import AlertmanagerWebhook, IncidentState


class ApprovalDecision(BaseModel):
    token: str
    decision: Literal["approve", "reject"]


def create_app(
    *,
    alert_token: str,
    manager: IncidentManager,
    confirmer: AlertConfirmer,
    approvals: ApprovalStore | None = None,
    on_decision: Callable[[Approval, bool], object] | None = None,
) -> FastAPI:
    if not alert_token:
        raise ValueError("an alert token is required; refusing to run an open ingest endpoint")

    app = FastAPI(title="Heimdall", docs_url=None, redoc_url=None, openapi_url=None)
    # Alerts are processed one at a time so correlation sees a consistent view.
    processing = threading.Lock()

    # A registry per app, so tests and restarts never share counters.
    registry = CollectorRegistry()
    alerts_total = Counter(
        "heimdall_alerts_total",
        "Alerts received, by whether Prometheus confirmed them.",
        ["outcome"],
        registry=registry,
    )
    rejected_total = Counter(
        "heimdall_ingest_rejected_total",
        "Requests to the ingest endpoint rejected for a bad or missing token.",
        registry=registry,
    )
    incidents_gauge = Gauge(
        "heimdall_incidents", "Incidents by current state.", ["state"], registry=registry
    )

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {alert_token}"
        if authorization is None or not hmac.compare_digest(
            authorization.encode(), expected.encode()
        ):
            rejected_total.inc()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing token")

    def process(payload: AlertmanagerWebhook) -> None:
        with processing:
            for alert in payload.alerts:
                confirmed = confirmer.confirm(alert)
                alerts_total.labels("confirmed" if confirmed else "unconfirmed").inc()
                manager.ingest(alert, confirmed=confirmed)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics() -> Response:
        counts = dict.fromkeys(IncidentState, 0)
        for incident in manager.all():
            counts[incident.state] += 1
        for state, count in counts.items():
            incidents_gauge.labels(state).set(count)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.post(
        "/alerts", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_token)]
    )
    def alerts(payload: AlertmanagerWebhook, background: BackgroundTasks) -> dict[str, int]:
        # Answer immediately; confirmation and correlation happen afterwards.
        background.add_task(process, payload)
        return {"accepted": len(payload.alerts)}

    if approvals is not None:
        # Reading shows what is being asked and changes nothing, so a chat
        # client that fetches the link for a preview cannot approve anything.
        # Deciding takes a POST with the token in the body.
        @app.get("/approvals/{approval_id}")
        def show_approval(approval_id: str, token: str = "") -> dict[str, object]:
            try:
                approval = approvals.authenticate(approval_id, token)
            except ApprovalError as e:
                raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
            return {
                "incident": approval.incident_id,
                "status": approval.status,
                "expires_at": approval.expires_at.isoformat(),
                "action": approval.proposal.model_dump(mode="json", exclude_none=True),
            }

        @app.post("/approvals/{approval_id}")
        def decide_approval(
            approval_id: str, body: ApprovalDecision, background: BackgroundTasks
        ) -> dict[str, str]:
            try:
                approval = approvals.decide(
                    approval_id, body.token, approve=body.decision == "approve"
                )
            except ApprovalError as e:
                raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
            if on_decision is not None:
                approved = approval.status is ApprovalStatus.APPROVED
                background.add_task(on_decision, approval, approved)
            return {"status": approval.status}

    return app
