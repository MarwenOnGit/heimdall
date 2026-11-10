"""Data models shared across Heimdall and the chaos suite."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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
