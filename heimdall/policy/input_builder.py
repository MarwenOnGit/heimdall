"""Builds the policy input for a proposal.

Everything the policy decides on comes from here, and nothing here comes from
model text: the current state is read from the cluster with the read-only
identity, the history from Heimdall's own action record. The model only
contributes the proposal itself.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from heimdall.core.actions import ActionStore
from heimdall.core.models import ActionProposal, ActionType, Diagnosis, ResourceRef
from heimdall.core.revisions import RevisionTracker
from heimdall.executor.gitops import GitOps, GitOpsError
from heimdall.tools.k8s import MESH_CONTAINERS
from heimdall.tools.kube import KubeClient, KubeError

STATEFUL_LABEL = "heimdall.io/stateful"


class CannotBuild(Exception):
    """The proposal cannot be evaluated at all; it is denied with this reason."""


class TargetMissing(CannotBuild):
    """The Deployment a proposal targets does not exist."""


@dataclass(frozen=True)
class PolicyInput:
    data: dict[str, Any]
    # The version of the target the policy decided on. The executor sends it
    # back as a precondition, so a change in between makes the write fail.
    resource_version: str | None


def _minutes(since: str, until: datetime) -> int:
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    return max(0, int((until - start).total_seconds() // 60))


class InputBuilder:
    def __init__(
        self,
        kube: KubeClient,
        actions: ActionStore,
        *,
        mode: str,
        gitops: GitOps | None = None,
        revisions: RevisionTracker | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._revisions_tracker = revisions
        self._kube = kube
        self._actions = actions
        self._mode = mode
        self._gitops = gitops
        self._clock = clock

    def _files_for(self, resource: ResourceRef) -> list[str]:
        if self._gitops is None:
            return []
        try:
            return self._gitops.files_for(resource)
        except GitOpsError as e:
            raise CannotBuild(str(e)) from e

    def build(self, proposal: ActionProposal, diagnosis: Diagnosis) -> PolicyInput:
        resources = [c.affected_resource for c in diagnosis.root_causes if c.affected_resource]
        data: dict[str, Any] = {
            "action": proposal.model_dump(
                mode="json", exclude_none=True, exclude={"justification", "expected_effect"}
            ),
            "incident": {
                "confidence": diagnosis.confidence,
                "affected_resources": [
                    {"kind": r.kind, "namespace": r.namespace, "name": r.name} for r in resources
                ],
                "affected_files": sorted({f for r in resources for f in self._files_for(r)}),
                "write_actions_so_far": self._actions.writes_so_far(diagnosis.incident_id),
            },
            "recent_actions": self._actions.recent(),
            "revision_history": [],
            "current": {},
            "mode": self._mode,
            "frozen": self._actions.frozen(),
        }
        version = None
        if proposal.type is ActionType.OPEN_PR and proposal.target is not None:
            # What the pull request would contain is worked out here, from
            # git, so the policy judges the real change and not a description.
            if self._gitops is None:
                data["action"].update(files=[], diff_lines=0)
            else:
                try:
                    revert = self._gitops.plan_revert(proposal.target)
                except GitOpsError as e:
                    raise CannotBuild(str(e)) from e
                data["action"].update(files=revert.files, diff_lines=revert.diff_lines)
        elif proposal.target is not None:
            deployment = self._deployment(proposal.target)
            version = deployment["metadata"].get("resourceVersion")
            data["current"] = self._current(deployment, proposal.container)
            data["revision_history"] = self._revisions(proposal.target, deployment)
        return PolicyInput(data, version)

    def _deployment(self, target: ResourceRef) -> dict[str, Any]:
        path = f"/apis/apps/v1/namespaces/{target.namespace}/deployments/{target.name}"
        try:
            return self._kube.get(path)
        except KubeError as e:
            if e.status in (403, 404):
                # 403: outside the namespaces the read-only identity can see.
                raise TargetMissing(f"Deployment {target.namespace}/{target.name}") from e
            raise

    def _current(self, deployment: dict[str, Any], container: str | None) -> dict[str, Any]:
        containers = [
            c
            for c in deployment["spec"]["template"]["spec"]["containers"]
            if c["name"] not in MESH_CONTAINERS
        ]
        chosen = next((c for c in containers if c["name"] == container), None)
        if chosen is None and containers:
            chosen = containers[0]
        limits = (chosen or {}).get("resources", {}).get("limits", {})
        labels = deployment["metadata"].get("labels", {})
        return {
            "replicas": deployment["spec"].get("replicas", 0),
            "stateful": labels.get(STATEFUL_LABEL) == "true",
            "limits": dict(limits),
            "resource_version": deployment["metadata"].get("resourceVersion"),
        }

    def _revisions(self, target: ResourceRef, deployment: dict[str, Any]) -> list[dict[str, Any]]:
        """How long each earlier revision was Available.

        Measured by the revision tracker where it has data. A revision that
        appeared while the tracker was watching and was never seen healthy
        gets no credit at all.

        Revisions older than anything the tracker has seen (deployed before
        Heimdall was installed) are estimated from ReplicaSets instead: a
        superseded revision is credited with the time until the next one was
        created. That over-credits a revision that was broken while it was
        current, which is why it is only the fallback. The current revision
        is never offered as a rollback target.
        """
        uid = deployment["metadata"].get("uid")
        current = (
            deployment["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision")
        )
        listing = self._kube.get(f"/apis/apps/v1/namespaces/{target.namespace}/replicasets")
        owned = []
        for rs in listing.get("items", []):
            revision = (
                rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision")
            )
            owners = rs["metadata"].get("ownerReferences", [])
            if revision and any(o.get("uid") == uid for o in owners):
                owned.append((int(revision), rs["metadata"]["creationTimestamp"]))
        owned.sort()

        measured: dict[int, float] = {}
        if self._revisions_tracker is not None and target.namespace is not None:
            measured = self._revisions_tracker.available_minutes(target.namespace, target.name)
        first_measured = min(measured, default=None)

        history = []
        for index, (revision, created) in enumerate(owned):
            if str(revision) == str(current):
                continue
            if first_measured is not None and revision >= first_measured:
                history.append(
                    {"revision": revision, "available_minutes": measured.get(revision, 0)}
                )
                continue
            if index + 1 >= len(owned):
                continue
            superseded_at = datetime.fromisoformat(owned[index + 1][1].replace("Z", "+00:00"))
            history.append(
                {"revision": revision, "available_minutes": _minutes(created, superseded_at)}
            )
        return history
