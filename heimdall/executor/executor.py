"""The executor: the only code that writes to the cluster.

It runs an action that the policy gate allowed, with the actor identity. Each
write is conditional on the object being unchanged since the policy looked at
it. In dry-run mode the request goes to the API server with `dryRun=All`: the
server runs it completely, admission policy included, and stores nothing, so
a dry run proves the action would really have been accepted.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from heimdall.core.actions import RAN, ActionStatus, ActionStore
from heimdall.core.audit import AuditLog, EventType
from heimdall.core.models import ActionProposal, ActionType
from heimdall.policy.gate import Decision
from heimdall.tools.kube import KubeClient, KubeError

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"


@dataclass(frozen=True)
class Outcome:
    status: ActionStatus
    detail: str
    # State of the target before the write; what an undo would restore.
    previous: dict[str, Any] | None = None


class Executor:
    def __init__(
        self,
        actor: KubeClient,
        reader: KubeClient,
        actions: ActionStore,
        audit: AuditLog,
        *,
        dry_run: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._actor = actor
        self._reader = reader
        self._actions = actions
        self._audit = audit
        self._dry_run = dry_run
        self._clock = clock
        self._locks: dict[tuple[str | None, str | None], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, proposal: ActionProposal) -> threading.Lock:
        """One lock per Deployment: two incidents never act on it at once."""
        with self._locks_guard:
            return self._locks.setdefault((proposal.namespace, proposal.name), threading.Lock())

    def execute(
        self,
        incident_id: str,
        proposal: ActionProposal,
        decision: Decision,
        resource_version: str | None,
    ) -> Outcome:
        """Carry out the gate's decision for one proposal and record it."""
        if decision.decision == "deny":
            outcome = Outcome(ActionStatus.DENIED, "; ".join(decision.reasons))
        elif decision.decision == "require_approval":
            outcome = Outcome(ActionStatus.AWAITING_APPROVAL, "; ".join(decision.reasons))
        elif proposal.target is None or proposal.type is ActionType.OPEN_PR:
            outcome = Outcome(ActionStatus.RECORDED, self._describe_passive(proposal))
        else:
            earlier = self._actions.find(incident_id, proposal)
            if earlier is not None and earlier.status in RAN:
                # A restart must never apply the same action twice.
                return Outcome(earlier.status, earlier.detail, earlier.previous)
            with self._lock(proposal):
                outcome = self._write(proposal, resource_version)

        self._actions.record(
            incident_id, proposal, decision=decision.decision, status=outcome.status,
            detail=outcome.detail, previous=outcome.previous,
        )  # fmt: skip
        self._audit.append(
            EventType.ACTION_EXECUTED,
            {
                "proposal": proposal.model_dump(mode="json", exclude_none=True),
                "decision": decision.decision,
                "status": outcome.status,
                "detail": outcome.detail,
                "dry_run": self._dry_run,
            },
            actor="executor",
            incident_id=incident_id,
        )
        return outcome

    def _describe_passive(self, proposal: ActionProposal) -> str:
        if proposal.type is ActionType.OPEN_PR:
            return "pull requests are not implemented yet; a human should make this change"
        return proposal.summary or proposal.justification

    # -------------------------------------------------------------- writes

    def _write(self, proposal: ActionProposal, resource_version: str | None) -> Outcome:
        base = f"/apis/apps/v1/namespaces/{proposal.namespace}/deployments/{proposal.name}"
        try:
            previous = self._snapshot(base)
            path, body, patch_type = self._request(proposal, base)
        except KubeError as e:
            return Outcome(ActionStatus.FAILED, f"could not prepare the change: {e.message}")
        if resource_version is not None:
            # Makes the write conditional: the API server answers 409 if the
            # object changed since the policy gate read it.
            body.setdefault("metadata", {})["resourceVersion"] = resource_version
        try:
            self._actor.patch(path, body, patch_type=patch_type, dry_run=self._dry_run)
        except KubeError as e:
            if e.status == 409:
                return Outcome(ActionStatus.CONFLICT, "the target changed after the policy check")
            if e.status in (403, 422):
                return Outcome(ActionStatus.REJECTED_BY_API, e.message)
            return Outcome(ActionStatus.FAILED, f"Kubernetes API returned {e.status}")
        if self._dry_run:
            return Outcome(
                ActionStatus.DRY_RUN, "accepted by the API server in dry-run mode", previous
            )
        return Outcome(ActionStatus.EXECUTED, "applied", previous)

    def _snapshot(self, base: str) -> dict[str, Any]:
        """The parts of a Deployment that an action can change."""
        deployment = self._reader.get(base)
        return {
            "replicas": deployment["spec"].get("replicas"),
            "containers": [
                {
                    "name": c["name"],
                    "image": c["image"],
                    "limits": c.get("resources", {}).get("limits", {}),
                }
                for c in deployment["spec"]["template"]["spec"]["containers"]
            ],
        }

    # ---------------------------------------------------------------- undo

    def undo(self, incident_id: str, proposal: ActionProposal) -> Outcome:
        """Put the target back as it was before `proposal` ran.

        The undo goes through the same actor identity, so RBAC and the
        admission policy apply to it too. It has no version precondition: the
        object changed precisely because of the action being undone.
        """
        record = self._actions.find(incident_id, proposal)
        if record is None or record.status is not ActionStatus.EXECUTED:
            return Outcome(ActionStatus.FAILED, "there is no executed action to undo")
        if proposal.type is ActionType.RESTART_DEPLOYMENT:
            outcome = Outcome(ActionStatus.UNDONE, "a restart leaves nothing to undo")
        elif record.previous is None:
            outcome = Outcome(ActionStatus.FAILED, "the previous state was not recorded")
        else:
            outcome = self._restore(proposal, record.previous)

        if outcome.status is ActionStatus.UNDONE:
            self._actions.record(
                incident_id, proposal, decision=record.decision, status=ActionStatus.UNDONE,
                detail=outcome.detail,
            )  # fmt: skip
        self._audit.append(
            EventType.ROLLBACK,
            {
                "proposal": proposal.model_dump(mode="json", exclude_none=True),
                "restored": record.previous,
                "status": outcome.status,
                "detail": outcome.detail,
            },
            actor="executor",
            incident_id=incident_id,
        )
        return outcome

    def _restore(self, proposal: ActionProposal, previous: dict[str, Any]) -> Outcome:
        base = f"/apis/apps/v1/namespaces/{proposal.namespace}/deployments/{proposal.name}"
        if proposal.type is ActionType.SCALE_DEPLOYMENT:
            path, patch_type = f"{base}/scale", "merge"
            body: dict[str, Any] = {"spec": {"replicas": previous["replicas"]}}
        elif proposal.type is ActionType.PATCH_RESOURCES:
            path, patch_type = base, "strategic-merge"
            body = _containers_patch(
                [{"name": c["name"], "resources": {"limits": c["limits"]}}
                 for c in previous["containers"]]
            )  # fmt: skip
        else:  # rollback_deployment
            path, patch_type = base, "strategic-merge"
            body = _containers_patch(
                [{"name": c["name"], "image": c["image"]} for c in previous["containers"]]
            )
        try:
            with self._lock(proposal):
                self._actor.patch(path, body, patch_type=patch_type, dry_run=self._dry_run)
        except KubeError as e:
            return Outcome(ActionStatus.FAILED, f"undo refused by the API server: {e.message}")
        return Outcome(ActionStatus.UNDONE, "previous state restored")

    def _request(self, proposal: ActionProposal, base: str) -> tuple[str, dict[str, Any], str]:
        """The path, body and patch type for a cluster write."""
        if proposal.type is ActionType.RESTART_DEPLOYMENT:
            stamp = self._clock().isoformat(timespec="seconds")
            annotations = {RESTART_ANNOTATION: stamp}
            return base, {"spec": {"template": {"metadata": {"annotations": annotations}}}}, "merge"

        if proposal.type is ActionType.SCALE_DEPLOYMENT:
            return f"{base}/scale", {"spec": {"replicas": proposal.replicas}}, "merge"

        if proposal.type is ActionType.PATCH_RESOURCES:
            container = {"name": proposal.container, "resources": {"limits": proposal.limits}}
            if proposal.container is None:
                container["name"] = self._containers(base)[0]["name"]
            return base, _containers_patch([container]), "strategic-merge"

        if proposal.type is ActionType.ROLLBACK_DEPLOYMENT:
            # A rollback restores the images of the target revision and
            # nothing else; that is all the admission policy lets through.
            images = self._revision_images(proposal)
            return base, _containers_patch(images), "strategic-merge"

        raise ValueError(f"{proposal.type} is not a cluster write")

    def _containers(self, base: str) -> list[dict[str, Any]]:
        containers: list[dict[str, Any]] = self._reader.get(base)["spec"]["template"]["spec"][
            "containers"
        ]
        return containers

    def _revision_images(self, proposal: ActionProposal) -> list[dict[str, Any]]:
        listing = self._reader.get(f"/apis/apps/v1/namespaces/{proposal.namespace}/replicasets")
        for rs in listing.get("items", []):
            annotations = rs["metadata"].get("annotations", {})
            owned = any(
                o.get("kind") == "Deployment" and o.get("name") == proposal.name
                for o in rs["metadata"].get("ownerReferences", [])
            )
            if owned and annotations.get("deployment.kubernetes.io/revision") == str(
                proposal.to_revision
            ):
                return [
                    {"name": c["name"], "image": c["image"]}
                    for c in rs["spec"]["template"]["spec"]["containers"]
                ]
        raise KubeError(404, f"revision {proposal.to_revision} not found")


def _containers_patch(containers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"spec": {"template": {"spec": {"containers": containers}}}}
