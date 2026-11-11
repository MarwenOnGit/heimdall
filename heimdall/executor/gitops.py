"""Keeping git and the cluster in step.

A live fix (a rollback, a resource patch) makes the cluster differ from git.
Left alone, the next unrelated commit would make Argo CD sync and re-apply the
broken state. So before such a fix Heimdall pauses automatic sync, and
afterwards it opens a pull request that makes git match: it reverts the
latest commit that touched the affected resource's files. A human merges it;
only then is the incident resolved and sync switched back on.
"""

import base64
from dataclasses import dataclass
from typing import Any

import httpx
import yaml

from heimdall.core.models import ResourceRef
from heimdall.tools.kube import KubeClient

KUSTOMIZATION = "kustomization.yaml"


class GitOpsError(Exception):
    pass


@dataclass(frozen=True)
class Revert:
    """What reverting the latest change to a resource would do."""

    commit: str
    message: str
    # path -> new content, or None to delete the file
    changes: dict[str, str | None]
    diff_lines: int

    @property
    def files(self) -> list[str]:
        return sorted(self.changes)


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    branch: str


class GitOps:
    def __init__(
        self,
        gitea: httpx.Client,
        repo: str,
        kube: KubeClient | None = None,
        *,
        root: str = "online-boutique",
        application: str = "boutique",
        argocd_namespace: str = "argocd",
    ) -> None:
        self._http = gitea
        self._repo = f"/api/v1/repos/{repo}"
        self._kube = kube
        self._root = root
        self._application = (
            f"/apis/argoproj.io/v1alpha1/namespaces/{argocd_namespace}/applications/{application}"
        )

    # -------------------------------------------------------------- git

    def _get(self, endpoint: str, **params: Any) -> httpx.Response:
        try:
            return self._http.get(f"{self._repo}{endpoint}", params=params)
        except httpx.HTTPError as e:
            raise GitOpsError("the git server is unreachable") from e

    def _send(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        try:
            response = self._http.request(method, f"{self._repo}{path}", json=body)
        except httpx.HTTPError as e:
            raise GitOpsError("the git server is unreachable") from e
        if not response.is_success and response.status_code != 409:
            raise GitOpsError(f"git server returned {response.status_code} for {method} {path}")
        return response

    def _read(self, path: str, ref: str = "main") -> tuple[str, str] | None:
        response = self._get(f"/contents/{path}", ref=ref)
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise GitOpsError(f"git server returned {response.status_code} reading {path}")
        body = response.json()
        return base64.b64decode(body["content"]).decode(), body["sha"]

    def files_for(self, resource: ResourceRef) -> list[str]:
        """Files in the repo that belong to a resource: its override patches,
        and the kustomization that lists them. A pull request for an incident
        may touch these and nothing else."""
        response = self._get(f"/contents/{self._root}/overrides", ref="main")
        if response.status_code == 404:
            return []
        if not response.is_success:
            raise GitOpsError(f"git server returned {response.status_code} listing overrides")
        mine = sorted(
            entry["path"]
            for entry in response.json()
            if entry.get("type") == "file" and entry["name"].startswith(f"{resource.name}-")
        )
        return [*mine, f"{self._root}/{KUSTOMIZATION}"] if mine else []

    def plan_revert(self, resource: ResourceRef) -> Revert:
        """Work out how to undo the latest commit that touched this resource's
        files: each file goes back to what it was before that commit."""
        files = [f for f in self.files_for(resource) if not f.endswith(KUSTOMIZATION)]
        if not files:
            raise GitOpsError(f"git has no change to {resource.name} to revert")

        latest: dict[str, Any] | None = None
        for path in files:
            response = self._get("/commits", sha="main", path=path, limit=1, stat="false")
            commits = response.json() if response.is_success else []
            if commits and (
                latest is None
                or commits[0]["commit"]["author"]["date"] > latest["commit"]["author"]["date"]
            ):
                latest = commits[0]
        if latest is None or not latest.get("parents"):
            raise GitOpsError(f"could not find the commit that changed {resource.name}")
        parent = latest["parents"][0]["sha"]

        changes: dict[str, str | None] = {}
        diff_lines = 0
        removed_patches = []
        for path in files:
            now = self._read(path)
            before = self._read(path, ref=parent)
            if now is None or (before is not None and before[0] == now[0]):
                continue  # this file was not changed by that commit
            changes[path] = before[0] if before else None
            diff_lines += len(now[0].splitlines()) + (len(before[0].splitlines()) if before else 0)
            if before is None:
                removed_patches.append(path.removeprefix(f"{self._root}/"))

        if removed_patches:
            # A patch file that goes away must also leave the kustomization,
            # or the whole application stops rendering.
            path = f"{self._root}/{KUSTOMIZATION}"
            current = self._read(path)
            if current is None:
                raise GitOpsError(f"{path} is missing")
            kustomization = yaml.safe_load(current[0])
            kept = [p for p in kustomization.get("patches", [])
                    if p.get("path") not in removed_patches]  # fmt: skip
            diff_lines += len(kustomization.get("patches", [])) - len(kept)
            kustomization["patches"] = kept
            changes[path] = yaml.safe_dump(kustomization, sort_keys=False)

        if not changes:
            raise GitOpsError(f"the latest commit did not change {resource.name}")
        subject = latest["commit"]["message"].strip().split("\n")[0]
        return Revert(latest["sha"], subject, changes, diff_lines)

    def open_revert_pr(self, incident_id: str, resource: ResourceRef, summary: str) -> PullRequest:
        revert = self.plan_revert(resource)
        branch = f"heimdall/{incident_id}"
        self._send("POST", "/branches", {"new_branch_name": branch, "old_branch_name": "main"})

        operations = []
        for path, content in revert.changes.items():
            existing = self._read(path, ref=branch)
            if existing is None:
                continue
            if content is None:
                operations.append({"operation": "delete", "path": path, "sha": existing[1]})
            elif existing[0] != content:
                operations.append(
                    {"operation": "update", "path": path, "sha": existing[1],
                     "content": base64.b64encode(content.encode()).decode()}
                )  # fmt: skip
        if operations:
            self._send(
                "POST", "/contents",
                {"branch": branch, "files": operations,
                 "message": f"Revert \"{revert.message}\"\n\nIncident {incident_id}."},
            )  # fmt: skip

        title = f'Revert "{revert.message}" ({incident_id})'
        body = (
            f"Heimdall diagnosed incident {incident_id}:\n\n{summary}\n\n"
            f"This reverts commit {revert.commit[:10]}, the latest change to {resource.name}. "
            "Merging it makes git match the mitigated state of the cluster."
        )
        response = self._send(
            "POST", "/pulls", {"head": branch, "base": "main", "title": title, "body": body}
        )
        if response.status_code == 409:
            # Already opened for this incident (for example before a restart).
            listing = self._get("/pulls", state="open")
            for pr in listing.json() if listing.is_success else []:
                if pr.get("head", {}).get("ref") == branch:
                    return PullRequest(pr["number"], pr["html_url"], branch)
            raise GitOpsError("a pull request for this incident exists but could not be found")
        pr = response.json()
        return PullRequest(pr["number"], pr["html_url"], branch)

    def merged(self, number: int) -> bool:
        response = self._get(f"/pulls/{number}")
        return response.is_success and bool(response.json().get("merged"))

    # ---------------------------------------------------------- argo cd

    def _set_sync(self, automated: dict[str, Any] | None) -> None:
        if self._kube is None:
            raise GitOpsError("no Kubernetes client configured for Argo CD")
        self._kube.patch(self._application, {"spec": {"syncPolicy": {"automated": automated}}})

    def pause_sync(self) -> None:
        """Stop Argo CD from syncing on its own, so a live fix is not
        overwritten by the next commit."""
        self._set_sync(None)

    def resume_sync(self) -> None:
        self._set_sync({"prune": True, "selfHeal": False})
