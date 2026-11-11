"""Runtime configuration, read from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    alert_token: str
    db_path: Path
    prometheus_url: str
    dependencies_path: Path
    audit_key_path: Path | None
    anchor_file: Path | None
    anchor_every_events: int
    gitea_url: str | None
    gitea_user: str | None
    gitea_token: str | None

    @classmethod
    def from_env(cls) -> "Config":
        def path(name: str) -> Path | None:
            value = os.environ.get(name)
            return Path(value) if value else None

        return cls(
            alert_token=os.environ.get("HEIMDALL_ALERT_TOKEN", ""),
            db_path=Path(os.environ.get("HEIMDALL_DB", "heimdall.sqlite")),
            prometheus_url=os.environ.get(
                "HEIMDALL_PROMETHEUS_URL", "http://kps-prometheus.monitoring.svc:9090"
            ),
            dependencies_path=Path(
                os.environ.get("HEIMDALL_DEPENDENCIES", "docs/dependencies.yaml")
            ),
            audit_key_path=path("HEIMDALL_AUDIT_KEY"),
            anchor_file=path("HEIMDALL_ANCHOR_FILE"),
            anchor_every_events=int(os.environ.get("HEIMDALL_ANCHOR_EVERY", "50")),
            gitea_url=os.environ.get("HEIMDALL_GITEA_URL"),
            gitea_user=os.environ.get("HEIMDALL_GITEA_USER"),
            gitea_token=os.environ.get("HEIMDALL_GITEA_TOKEN"),
        )
