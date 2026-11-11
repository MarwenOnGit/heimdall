"""Data models shared across Heimdall and the chaos suite."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RootCauseCategory(StrEnum):
    BAD_CONFIG = "bad_config"
    MISSING_DEPENDENCY = "missing_dependency"
    RESOURCE_LIMITS_TOO_LOW = "resource_limits_too_low"
    BAD_RELEASE = "bad_release"
    BAD_IMAGE_REFERENCE = "bad_image_reference"
    BAD_PROBE_CONFIG = "bad_probe_config"
    DEPENDENCY_DOWN = "dependency_down"
    RESOURCE_SATURATION = "resource_saturation"
    NETWORK_DEGRADATION = "network_degradation"
    DISK_PRESSURE = "disk_pressure"
    NODE_PROBLEM = "node_problem"
    NO_ACTION_NEEDED = "no_action_needed"
    UNKNOWN = "unknown"


class ActionType(StrEnum):
    RESTART_DEPLOYMENT = "restart_deployment"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    SCALE_DEPLOYMENT = "scale_deployment"
    PATCH_RESOURCES = "patch_resources"
    OPEN_PR = "open_pr"
    ESCALATE = "escalate"
    NONE = "none"


class ResourceRef(BaseModel):
    """A Kubernetes object. `namespace` is None for cluster-scoped kinds."""

    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    namespace: str | None = None


class Alert(BaseModel):
    """One alert as delivered by the Alertmanager webhook."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    fingerprint: str

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "")

    @property
    def namespace(self) -> str | None:
        return self.labels.get("namespace")

    @property
    def service(self) -> str | None:
        return self.labels.get("service")


class AlertmanagerWebhook(BaseModel):
    """The Alertmanager webhook payload (version 4)."""

    model_config = ConfigDict(extra="ignore")

    version: str
    status: str
    receiver: str
    alerts: list[Alert]


class IncidentState(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    DIAGNOSED = "diagnosed"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


TERMINAL_STATES = frozenset({IncidentState.RESOLVED, IncidentState.ESCALATED, IncidentState.FAILED})


class Incident(BaseModel):
    id: str
    state: IncidentState
    namespace: str | None
    services: list[str]
    flapping: bool = False
    opened_at: datetime
    updated_at: datetime
