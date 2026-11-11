"""Read-only Kubernetes tools for the investigator.

Output is compact `key=value` text, one object per line where possible, so the
model can quote a line verbatim as evidence. Env var values and ConfigMap
contents are never returned: names only.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from heimdall.tools.kube import KubeClient, KubeError
from heimdall.tools.registry import Tool, ToolError

MESH_CONTAINERS = {"linkerd-proxy", "linkerd-init"}
# Volumes injected by Kubernetes and the mesh; noise for an investigation.
PLUMBING_VOLUMES = ("kube-api-access-", "linkerd-")


def _age(timestamp: str | None, now: datetime) -> str:
    if not timestamp:
        return "?"
    seconds = int((now - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds())
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _state(status: dict[str, Any]) -> str:
    """Summarize a container status: state, reason and exit code."""
    state = status.get("state", {})
    if "running" in state:
        return "running"
    for kind in ("waiting", "terminated"):
        if kind in state:
            detail = state[kind]
            parts = [kind, f"reason={detail.get('reason', '?')}"]
            if "exitCode" in detail:
                parts.append(f"exit={detail['exitCode']}")
            if detail.get("message"):
                parts.append(f"message={detail['message']!r}")
            return " ".join(parts)
    return "unknown"


def _last_termination(status: dict[str, Any]) -> str | None:
    last = status.get("lastState", {}).get("terminated")
    if not last:
        return None
    return f"{last.get('reason', '?')}(exit={last.get('exitCode', '?')})"


def _resources(container: dict[str, Any]) -> str:
    res = container.get("resources", {})
    req, lim = res.get("requests", {}), res.get("limits", {})
    return (
        f"requests(cpu={req.get('cpu', '-')},memory={req.get('memory', '-')}) "
        f"limits(cpu={lim.get('cpu', '-')},memory={lim.get('memory', '-')})"
    )


def _probe(probe: dict[str, Any] | None) -> str:
    if not probe:
        return "none"
    for kind in ("httpGet", "grpc", "tcpSocket", "exec"):
        if kind in probe:
            target = probe[kind]
            if kind == "httpGet":
                return f"http:{target.get('port')}{target.get('path', '')}"
            if kind == "exec":
                return "exec"
            return f"{kind.removesuffix('Socket')}:{target.get('port')}"
    return "unknown"


def _volume_type(volume: dict[str, Any]) -> str:
    return next((k for k in volume if k != "name"), "unknown")


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """Flatten nested JSON into path -> value, with env values masked."""
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            label = item.get("name", index) if isinstance(item, dict) else index
            out.update(_flatten(item, f"{prefix}[{label}]"))
    else:
        out[prefix] = str(value)
    return out


def _is_env_value(path: str) -> bool:
    return ".env[" in path and not path.endswith(".name")


class Namespaced(BaseModel):
    namespace: str = Field(description="Kubernetes namespace")


class ListPodsArgs(Namespaced):
    label_selector: str | None = Field(
        default=None, description="e.g. app.kubernetes.io/name=cartservice"
    )


class PodArgs(Namespaced):
    name: str = Field(description="Pod name")


class EventsArgs(Namespaced):
    involved_object: str | None = Field(
        default=None, description="Only events whose object name contains this text"
    )
    since_minutes: int = Field(default=30, ge=1, le=1440)


class LogsArgs(Namespaced):
    pod: str
    container: str | None = Field(default=None, description="Default: the application container")
    tail: int = Field(default=200, ge=1, le=2000)
    previous: bool = Field(default=False, description="Logs of the previous, crashed container")


class DeploymentArgs(Namespaced):
    name: str = Field(description="Deployment name")


class DiffArgs(DeploymentArgs):
    from_revision: int
    to_revision: int


class NoArgs(BaseModel):
    pass


class KubernetesTools:
    def __init__(
        self,
        client: KubeClient,
        allowed_namespaces: set[str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._kube = client
        self._allowed = allowed_namespaces
        self._clock = clock

    def _ns(self, namespace: str) -> str:
        if namespace not in self._allowed:
            raise ToolError(
                f"namespace {namespace!r} is not allowed; allowed: {sorted(self._allowed)}"
            )
        return namespace

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        try:
            return self._kube.get(path, **params)
        except KubeError as e:
            if e.status == 404:
                raise ToolError("not found") from e
            raise ToolError(f"Kubernetes API returned {e.status}") from e

    # ---------------------------------------------------------------- pods

    def list_pods(self, args: ListPodsArgs) -> str:
        ns = self._ns(args.namespace)
        pods = self._get(f"/api/v1/namespaces/{ns}/pods", labelSelector=args.label_selector)
        now = self._clock()
        lines = []
        for pod in sorted(pods["items"], key=lambda p: p["metadata"]["name"]):
            statuses = [
                s
                for s in pod.get("status", {}).get("containerStatuses", [])
                if s["name"] not in MESH_CONTAINERS
            ]
            ready = sum(1 for s in statuses if s.get("ready"))
            line = (
                f"pod={pod['metadata']['name']} phase={pod['status'].get('phase')} "
                f"ready={ready}/{len(statuses)} "
                f"restarts={sum(s.get('restartCount', 0) for s in statuses)} "
                f"node={pod['spec'].get('nodeName', '-')} "
                f"age={_age(pod['metadata'].get('creationTimestamp'), now)}"
            )
            waiting = [
                s["state"]["waiting"].get("reason") for s in statuses if "waiting" in s["state"]
            ]
            if waiting:
                line += f" waiting={','.join(filter(None, waiting))}"
            terminated = [t for t in map(_last_termination, statuses) if t]
            if terminated:
                line += f" last_termination={','.join(terminated)}"
            if pod["metadata"].get("deletionTimestamp"):
                line += " terminating=true"
            lines.append(line)
        return "\n".join(lines) or "no pods found"

    def describe_pod(self, args: PodArgs) -> str:
        ns = self._ns(args.namespace)
        pod = self._get(f"/api/v1/namespaces/{ns}/pods/{args.name}")
        spec, status = pod["spec"], pod.get("status", {})
        lines = [
            f"pod={args.name} phase={status.get('phase')} node={spec.get('nodeName', '-')} "
            f"service_account={spec.get('serviceAccountName', '-')}"
        ]
        for cond in status.get("conditions", []):
            extra = f" reason={cond['reason']}" if cond.get("reason") else ""
            message = f" message={cond['message']!r}" if cond.get("message") else ""
            lines.append(f"condition {cond['type']}={cond['status']}{extra}{message}")
        by_name = {s["name"]: s for s in status.get("containerStatuses", [])}
        for container in spec.get("containers", []):
            name = container["name"]
            if name in MESH_CONTAINERS:
                continue
            lines.append(f"container={name} image={container['image']} {_resources(container)}")
            lines.append(
                f"  probes readiness={_probe(container.get('readinessProbe'))} "
                f"liveness={_probe(container.get('livenessProbe'))}"
            )
            env = [e["name"] for e in container.get("env", [])]
            lines.append(f"  env_names={','.join(env) or '-'} (values are never shown)")
            mounts = [
                f"{m['name']}:{m['mountPath']}"
                for m in container.get("volumeMounts", [])
                if not m["name"].startswith(PLUMBING_VOLUMES)
            ]
            lines.append(f"  mounts={','.join(mounts) or '-'}")
            if name in by_name:
                cs = by_name[name]
                line = (
                    f"  state={_state(cs)} ready={cs.get('ready')} "
                    f"restarts={cs.get('restartCount', 0)}"
                )
                if _last_termination(cs):
                    line += f" last_termination={_last_termination(cs)}"
                lines.append(line)
        volumes = [
            f"{v['name']}({_volume_type(v)})"
            for v in spec.get("volumes", [])
            if not v["name"].startswith(PLUMBING_VOLUMES)
        ]
        lines.append(f"volumes={','.join(volumes) or '-'}")
        return "\n".join(lines)

    def get_pod_logs(self, args: LogsArgs) -> str:
        ns = self._ns(args.namespace)
        container = args.container
        if container is None:
            pod = self._get(f"/api/v1/namespaces/{ns}/pods/{args.pod}")
            names = [
                c["name"] for c in pod["spec"]["containers"] if c["name"] not in MESH_CONTAINERS
            ]
            container = names[0] if names else None
        try:
            text = self._kube.get_text(
                f"/api/v1/namespaces/{ns}/pods/{args.pod}/log",
                container=container,
                tailLines=args.tail,
                previous="true" if args.previous else None,
            )
        except KubeError as e:
            if e.status == 404:
                raise ToolError("pod not found") from e
            if e.status == 400:
                # For example: no previous container, or still creating.
                raise ToolError(f"logs unavailable: {e.message}") from e
            raise ToolError(f"Kubernetes API returned {e.status}") from e
        return text.rstrip("\n") or "(no log lines)"

    # -------------------------------------------------------------- events

    def get_events(self, args: EventsArgs) -> str:
        ns = self._ns(args.namespace)
        events = self._get(f"/api/v1/namespaces/{ns}/events")["items"]
        cutoff = self._clock() - timedelta(minutes=args.since_minutes)

        def when(event: dict[str, Any]) -> str:
            return (
                event.get("lastTimestamp")
                or event.get("eventTime")
                or event["metadata"].get("creationTimestamp")
                or ""
            )

        lines = []
        for event in sorted(events, key=when):
            stamp = when(event)
            if not stamp or datetime.fromisoformat(stamp.replace("Z", "+00:00")) < cutoff:
                continue
            obj = event.get("involvedObject", {})
            if args.involved_object and args.involved_object not in obj.get("name", ""):
                continue
            lines.append(
                f"{stamp[:19]}Z type={event.get('type')} reason={event.get('reason')} "
                f"object={obj.get('kind')}/{obj.get('name')} count={event.get('count', 1)} "
                f"message={event.get('message', '')!r}"
            )
        return "\n".join(lines) or "no events in this period"

    # --------------------------------------------------------- deployments

    def get_deployment(self, args: DeploymentArgs) -> str:
        ns = self._ns(args.namespace)
        deploy = self._get(f"/apis/apps/v1/namespaces/{ns}/deployments/{args.name}")
        spec, status = deploy["spec"], deploy.get("status", {})
        annotations = deploy["metadata"].get("annotations", {})
        revision = annotations.get("deployment.kubernetes.io/revision")
        labels = ",".join(
            f"{k}={v}" for k, v in sorted(deploy["metadata"].get("labels", {}).items())
        )
        lines = [
            f"deployment={args.name} revision={revision} "
            f"replicas desired={spec.get('replicas')} ready={status.get('readyReplicas', 0)} "
            f"updated={status.get('updatedReplicas', 0)} "
            f"available={status.get('availableReplicas', 0)} "
            f"unavailable={status.get('unavailableReplicas', 0)}",
            f"strategy={spec.get('strategy', {}).get('type')} labels={labels}",
        ]
        for container in spec["template"]["spec"]["containers"]:
            lines.append(
                f"container={container['name']} image={container['image']} {_resources(container)}"
            )
        for cond in status.get("conditions", []):
            lines.append(
                f"condition {cond['type']}={cond['status']} reason={cond.get('reason')} "
                f"message={cond.get('message', '')!r}"
            )
        return "\n".join(lines)

    def _revisions(self, ns: str, name: str) -> dict[int, dict[str, Any]]:
        deploy = self._get(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")
        uid = deploy["metadata"]["uid"]
        replicasets = self._get(f"/apis/apps/v1/namespaces/{ns}/replicasets")["items"]
        revisions = {}
        for rs in replicasets:
            owners = rs["metadata"].get("ownerReferences", [])
            revision = (
                rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision")
            )
            if revision and any(o.get("uid") == uid for o in owners):
                revisions[int(revision)] = rs
        return revisions

    def get_rollout_history(self, args: DeploymentArgs) -> str:
        ns = self._ns(args.namespace)
        revisions = self._revisions(ns, args.name)
        if not revisions:
            return "no revisions found"
        current = max(revisions)
        lines = []
        for revision in sorted(revisions):
            rs = revisions[revision]
            images = ",".join(c["image"] for c in rs["spec"]["template"]["spec"]["containers"])
            lines.append(
                f"revision={revision}{' (current)' if revision == current else ''} "
                f"created={rs['metadata']['creationTimestamp']} image={images} "
                f"replicas={rs['spec'].get('replicas', 0)} "
                f"ready={rs.get('status', {}).get('readyReplicas', 0)}"
            )
        return "\n".join(lines)

    def diff_revisions(self, args: DiffArgs) -> str:
        ns = self._ns(args.namespace)
        revisions = self._revisions(ns, args.name)
        missing = [r for r in (args.from_revision, args.to_revision) if r not in revisions]
        if missing:
            raise ToolError(f"revision {missing[0]} not found; available: {sorted(revisions)}")

        def template(revision: int) -> dict[str, str]:
            spec = revisions[revision]["spec"]["template"]
            meta = spec.get("metadata", {})
            # The hash label always differs between revisions; it is noise.
            labels = {k: v for k, v in meta.get("labels", {}).items() if k != "pod-template-hash"}
            flat = _flatten({"labels": labels, "annotations": meta.get("annotations", {}),
                             "spec": spec["spec"]})  # fmt: skip
            # An env var's name is already in its path.
            return {p: v for p, v in flat.items() if not (".env[" in p and p.endswith(".name"))}

        old, new = template(args.from_revision), template(args.to_revision)
        lines = []
        for path in sorted(old.keys() | new.keys()):
            before, after = old.get(path), new.get(path)
            if before == after:
                continue
            # Compare the real values, but never print an env value.
            if _is_env_value(path):
                before = None if before is None else "<masked>"
                after = None if after is None else "<masked>"
            if before is None:
                lines.append(f"+ {path}: {after}")
            elif after is None:
                lines.append(f"- {path}: {before}")
            elif _is_env_value(path):
                lines.append(f"~ {path}: <masked> (value changed)")
            else:
                lines.append(f"~ {path}: {before} -> {after}")
        return "\n".join(lines) or "no differences in the pod template"

    # --------------------------------------------------------------- nodes

    def get_node_status(self, args: NoArgs) -> str:
        nodes = self._get("/api/v1/nodes")["items"]
        lines = []
        for node in sorted(nodes, key=lambda n: n["metadata"]["name"]):
            conditions = {c["type"]: c for c in node.get("status", {}).get("conditions", [])}
            ready = conditions.get("Ready", {})
            pressure = [
                t for t, c in conditions.items() if t != "Ready" and c.get("status") == "True"
            ]
            allocatable = node["status"].get("allocatable", {})
            lines.append(
                f"node={node['metadata']['name']} ready={ready.get('status', 'Unknown')} "
                f"reason={ready.get('reason', '-')} "
                f"last_heartbeat={ready.get('lastHeartbeatTime', '-')} "
                f"pressure={','.join(pressure) or 'none'} "
                f"unschedulable={node['spec'].get('unschedulable', False)} "
                f"allocatable(cpu={allocatable.get('cpu')},memory={allocatable.get('memory')})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------ registry

    def tools(self) -> list[Tool]:
        return [
            Tool("list_pods", "List pods with phase, readiness, restarts, node and the reason "
                 "for the last termination.", ListPodsArgs, self.list_pods),
            Tool("describe_pod", "Show one pod: containers, images, resources, probes, env var "
                 "names (never values), volumes, conditions and container states.",
                 PodArgs, self.describe_pod),
            Tool("get_pod_logs", "Read the last lines of a pod's logs. Use previous=true for the "
                 "container that crashed.", LogsArgs, self.get_pod_logs),
            Tool("get_events", "List recent Kubernetes events in a namespace, oldest first.",
                 EventsArgs, self.get_events),
            Tool("get_deployment", "Show a Deployment: replica counts, current revision, images, "
                 "resources and conditions.", DeploymentArgs, self.get_deployment),
            Tool("get_rollout_history", "List the revisions of a Deployment with their images "
                 "and creation times.", DeploymentArgs, self.get_rollout_history),
            Tool("diff_revisions", "Show what changed in the pod template between two revisions "
                 "of a Deployment. Env var values are masked.", DiffArgs, self.diff_revisions),
            Tool("get_node_status", "Show every node: readiness, pressure conditions and "
                 "allocatable resources.", NoArgs, self.get_node_status),
        ]  # fmt: skip
