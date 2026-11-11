"""HTTP API: alert ingest."""

import hmac
import threading
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status

from heimdall.core.confirm import AlertConfirmer
from heimdall.core.incidents import IncidentManager
from heimdall.core.models import AlertmanagerWebhook


def create_app(*, alert_token: str, manager: IncidentManager, confirmer: AlertConfirmer) -> FastAPI:
    if not alert_token:
        raise ValueError("an alert token is required; refusing to run an open ingest endpoint")

    app = FastAPI(title="Heimdall", docs_url=None, redoc_url=None, openapi_url=None)
    # Alerts are processed one at a time so correlation sees a consistent view.
    processing = threading.Lock()

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {alert_token}"
        if authorization is None or not hmac.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing token")

    def process(payload: AlertmanagerWebhook) -> None:
        with processing:
            for alert in payload.alerts:
                manager.ingest(alert, confirmed=confirmer.confirm(alert))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/alerts", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_token)]
    )
    def alerts(payload: AlertmanagerWebhook, background: BackgroundTasks) -> dict[str, int]:
        # Answer immediately; confirmation and correlation happen afterwards.
        background.add_task(process, payload)
        return {"accepted": len(payload.alerts)}

    return app
