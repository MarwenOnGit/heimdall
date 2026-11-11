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


class Evidence(BaseModel):
    """One observation backing a root cause, tied to the tool output it came from."""

    tool_call_id: str = Field(description="The id of the tool call, e.g. tc_3")
    quote: str = Field(min_length=3, description="Text copied exactly from that tool call's output")
    observation: str = Field(description="What the quote shows, in your own words")


class RootCause(BaseModel):
    category: RootCauseCategory
    affected_resource: ResourceRef | None = Field(
        default=None, description="The object that must change to fix the problem"
    )
    summary: str
    evidence: list[Evidence] = []
    confidence: float = Field(ge=0, le=1)


class RuledOut(BaseModel):
    hypothesis: str
    reason: str
    tool_call_id: str | None = None


class Diagnosis(BaseModel):
    incident_id: str
    root_causes: list[RootCause] = Field(min_length=1)
    ruled_out: list[RuledOut] = []

    @property
    def confidence(self) -> float:
        """An incident is only as well understood as its least certain cause."""
        return min(cause.confidence for cause in self.root_causes)


class ActionProposal(BaseModel):
    """One action the planner proposes. Only the executor ever runs it, and
    only after the policy gate has decided."""

    type: ActionType
    namespace: str | None = None
    name: str | None = Field(default=None, description="Deployment name")
    replicas: int | None = None
    to_revision: int | None = None
    container: str | None = None
    limits: dict[str, str] | None = Field(
        default=None, description='New limits, e.g. {"memory": "256Mi"}'
    )
    summary: str | None = Field(default=None, description="For escalate, none and open_pr")
    justification: str = Field(description="Why this action fixes the diagnosed cause")
    expected_effect: str = Field(
        description="A checkable outcome, e.g. frontend success rate above 99% within 3 minutes"
    )

    @property
    def target(self) -> ResourceRef | None:
        if self.name is None:
            return None
        return ResourceRef(kind="Deployment", namespace=self.namespace, name=self.name)
