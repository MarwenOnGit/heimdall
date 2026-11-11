"""A small stand-in for the Kubernetes API, for input-builder, executor and
pipeline tests. It serves one namespace of Deployments and their ReplicaSets
and records every PATCH it receives."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from heimdall.tools.kube import KubeClient

IMAGE_V1 = "repo/frontend:v0.10.7"
IMAGE_V2 = "repo/frontend:v0.11.1"


def deployment(
    name: str,
    *,
    image: str = IMAGE_V2,
    replicas: int = 1,
    revision: int = 2,
    stateful: bool = False,
    resource_version: str = "100",
) -> dict[str, Any]:
    labels = {"app.kubernetes.io/name": name}
    if stateful:
        labels["heimdall.io/stateful"] = "true"
    return {
        "metadata": {
            "name": name,
            "namespace": "boutique",
            "uid": f"uid-{name}",
            "resourceVersion": resource_version,
            "labels": labels,
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
        },
        "spec": {
            "replicas": replicas,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "server",
                            "image": image,
                            "resources": {"limits": {"memory": "128Mi", "cpu": "200m"}},
                        }
                    ]
                }
            },
        },
    }


def replicaset(name: str, revision: int, image: str, created: str) -> dict[str, Any]:
    return {
        "metadata": {
            "name": f"{name}-rs{revision}",
            "creationTimestamp": created,
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
            "ownerReferences": [{"kind": "Deployment", "name": name, "uid": f"uid-{name}"}],
        },
        "spec": {"template": {"spec": {"containers": [{"name": "server", "image": image}]}}},
    }


@dataclass
class FakeKube:
    deployments: dict[str, dict[str, Any]] = field(default_factory=dict)
    replicasets: list[dict[str, Any]] = field(default_factory=list)
    # Set to (status, message) to make the next PATCH fail that way, as RBAC
    # or the admission policy would.
    reject_with: tuple[int, str] | None = None
    patches: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, **kwargs: Any) -> None:
        self.deployments[name] = deployment(name, **kwargs)
        self.replicasets += [
            replicaset(name, 1, IMAGE_V1, "2026-11-09T09:00:00Z"),
            replicaset(name, 2, IMAGE_V2, "2026-11-10T13:02:00Z"),
        ]

    def client(self) -> KubeClient:
        return KubeClient(
            httpx.Client(transport=httpx.MockTransport(self._handle), base_url="https://kube")
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        # /apis/apps/v1/namespaces/<ns>/<kind>[/<name>[/scale]]
        namespace, kind = parts[4], parts[5]
        name = parts[6] if len(parts) > 6 else None
        if namespace != "boutique":
            return httpx.Response(403, json={"message": "forbidden"})
        if kind == "replicasets":
            return httpx.Response(200, json={"items": self.replicasets})
        if name not in self.deployments:
            return httpx.Response(404, json={"message": f'deployments.apps "{name}" not found'})
        current = self.deployments[name]
        if request.method == "GET":
            return httpx.Response(200, json=current)

        body = json.loads(request.content)
        self.patches.append(
            {
                "path": request.url.path,
                "body": body,
                "dry_run": request.url.params.get("dryRun") == "All",
                "content_type": request.headers["content-type"],
            }
        )
        wanted = body.get("metadata", {}).get("resourceVersion")
        if wanted is not None and wanted != current["metadata"]["resourceVersion"]:
            return httpx.Response(409, json={"message": "the object has been modified"})
        if self.reject_with is not None:
            status, message = self.reject_with
            return httpx.Response(status, json={"message": message})
        return httpx.Response(200, json=current)
