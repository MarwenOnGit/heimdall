"""Thin access layer to the lab cluster for the chaos runner.

Everything goes through kubectl with an explicit context, so the runner can
never act on another cluster that happens to be the current context.
"""

import base64
import json
import re
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "kind-heimdall"
NAMESPACE = "boutique"
PROMETHEUS = "/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy"
GITOPS_REPO = "heimdall/gitops"
KUSTOMIZATION = "online-boutique/kustomization.yaml"


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def promql(expr: str) -> list[dict[str, Any]]:
    """Run an instant query and return the result vector."""
    raw = kubectl("get", "--raw", f"{PROMETHEUS}/api/v1/query?query={quote(expr, safe='')}")
    data = json.loads(raw)
    if data.get("status") != "success":
        raise RuntimeError(f"query failed: {expr}: {data}")
    result: list[dict[str, Any]] = data["data"]["result"]
    return result


def firing_alerts() -> set[tuple[str, str | None]]:
    """Alerts routed to Heimdall that are firing now, as (alertname, service)."""
    rows = promql('ALERTS{heimdall="true", alertstate="firing"}')
    return {(r["metric"]["alertname"], r["metric"].get("service")) for r in rows}


def workload_problems() -> list[str]:
    """Reasons the workload is not at baseline. Empty means healthy."""
    problems: list[str] = []
    app = json.loads(kubectl("-n", "argocd", "get", "application", "boutique", "-o", "json"))
    status = app.get("status", {})
    sync = status.get("sync", {}).get("status")
    health = status.get("health", {}).get("status")
    if (sync, health) != ("Synced", "Healthy"):
        problems.append(f"application is {sync}/{health}")
    deployments = json.loads(kubectl("-n", NAMESPACE, "get", "deploy", "-o", "json"))["items"]
    for d in deployments:
        s = d.get("status", {})
        counts = (
            d["spec"].get("replicas"),
            s.get("replicas", 0),
            s.get("readyReplicas", 0),
            s.get("updatedReplicas", 0),
        )
        if counts != (1, 1, 1, 1):
            problems.append(f"deployment {d['metadata']['name']} replicas {counts}")
    return problems


def wait_for_baseline(timeout: float) -> list[str]:
    """Wait until the workload is healthy and no Heimdall alert is firing.

    Returns the remaining problems; an empty list means baseline was reached.
    """
    deadline = time.monotonic() + timeout
    while True:
        problems = workload_problems()
        problems += [f"alert {a}/{s} still firing" for a, s in sorted(firing_alerts(), key=str)]
        if not problems or time.monotonic() > deadline:
            return problems
        time.sleep(5)


def reset() -> None:
    subprocess.run([str(ROOT / "infra" / "reset.sh")], check=True, capture_output=True)


class Gitea:
    """Client for the in-cluster Gitea, used to commit to the GitOps repo."""

    def __init__(self, base_url: str, user: str, password: str) -> None:
        self._http = httpx.Client(
            base_url=f"{base_url}/api/v1/repos/{GITOPS_REPO}", auth=(user, password), timeout=30
        )

    def _read(self, path: str) -> tuple[str, str] | None:
        r = self._http.get(f"/contents/{path}", params={"ref": "main"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        body = r.json()
        return base64.b64decode(body["content"]).decode(), body["sha"]

    def commit(self, message: str, files: dict[str, str], patches: list[str], author: str) -> str:
        """Commit `files` to main and register `patches` in the kustomization.

        Returns the commit SHA.
        """
        changes = dict(files)
        if patches:
            current = self._read(KUSTOMIZATION)
            if current is None:
                raise RuntimeError(f"{KUSTOMIZATION} is missing from the GitOps repo")
            kustomization = yaml.safe_load(current[0])
            listed = kustomization.setdefault("patches", [])
            for path in patches:
                if {"path": path} not in listed:
                    listed.append({"path": path})
            changes[KUSTOMIZATION] = yaml.safe_dump(kustomization, sort_keys=False)

        operations = []
        for path, content in changes.items():
            existing = self._read(path)
            op: dict[str, str] = {
                "path": path,
                "content": base64.b64encode(content.encode()).decode(),
                "operation": "update" if existing else "create",
            }
            if existing:
                op["sha"] = existing[1]
            operations.append(op)

        identity = {"name": author, "email": f"{author}@boutique.example"}
        r = self._http.post(
            "/contents",
            json={
                "branch": "main",
                "message": message,
                "files": operations,
                "author": identity,
                "committer": identity,
            },
        )
        r.raise_for_status()
        sha: str = r.json()["commit"]["sha"]
        return sha


@contextmanager
def gitea() -> Iterator[Gitea]:
    """Open a port-forward to Gitea for the duration of the block."""

    def secret(key: str) -> str:
        raw = kubectl(
            "-n", "gitea", "get", "secret", "gitea-admin", "-o", f"jsonpath={{.data.{key}}}"
        )
        return base64.b64decode(raw).decode()

    forward = subprocess.Popen(
        ["kubectl", "--context", CONTEXT, "-n", "gitea", "port-forward",
         "--address", "127.0.0.1", "svc/gitea-http", ":3000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )  # fmt: skip
    try:
        assert forward.stdout is not None  # noqa: S101
        line = forward.stdout.readline()
        match = re.search(r"127\.0\.0\.1:(\d+)", line)
        if not match:
            raise RuntimeError(f"could not port-forward to Gitea: {line!r}")
        yield Gitea(f"http://127.0.0.1:{match.group(1)}", secret("username"), secret("password"))
    finally:
        forward.terminate()
        forward.wait(timeout=10)
