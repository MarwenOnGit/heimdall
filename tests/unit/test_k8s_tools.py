import json
from datetime import UTC, datetime

import httpx
import pytest

from heimdall.tools import k8s
from heimdall.tools.kube import KubeClient
from heimdall.tools.registry import ToolError

NOW = datetime(2026, 11, 10, 13, 10, 0, tzinfo=UTC)


def container(name: str = "server", image: str = "repo/cartservice:v1", **extra) -> dict:
    return {
        "name": name,
        "image": image,
        "resources": {"requests": {"cpu": "200m", "memory": "64Mi"},
                      "limits": {"cpu": "300m", "memory": "128Mi"}},
        "env": [{"name": "REDIS_ADDR", "value": "redis-cart:6379"},
                {"name": "API_TOKEN", "value": "super-secret-value"}],
        "readinessProbe": {"grpc": {"port": 7070}},
        "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
        "volumeMounts": [{"name": "kube-api-access-x", "mountPath": "/var/run/secrets"},
                         {"name": "settings", "mountPath": "/etc/settings"}],
        **extra,
    }  # fmt: skip


CRASHING_POD = {
    "metadata": {"name": "cartservice-abc", "creationTimestamp": "2026-11-10T13:00:00Z",
                 "labels": {"app.kubernetes.io/name": "cartservice"}},
    "spec": {
        "nodeName": "heimdall-worker2",
        "serviceAccountName": "cartservice",
        "containers": [container(), container("linkerd-proxy", "cr.l5d.io/linkerd/proxy:x")],
        "volumes": [{"name": "kube-api-access-x", "projected": {}},
                    {"name": "settings", "configMap": {"name": "cart-settings"}}],
    },
    "status": {
        "phase": "Running",
        "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady",
                        "message": "containers with unready status: [server]"}],
        "containerStatuses": [
            {"name": "server", "ready": False, "restartCount": 5,
             "state": {"waiting": {"reason": "CrashLoopBackOff",
                                   "message": "back-off 2m40s restarting failed container"}},
             "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}},
            {"name": "linkerd-proxy", "ready": True, "restartCount": 0, "state": {"running": {}}},
        ],
    },
}  # fmt: skip

HEALTHY_POD = {
    "metadata": {"name": "adservice-xyz", "creationTimestamp": "2026-11-08T13:10:00Z"},
    "spec": {"nodeName": "heimdall-worker2", "containers": [container(image="repo/adservice:v1")]},
    "status": {"phase": "Running", "containerStatuses": [
        {"name": "server", "ready": True, "restartCount": 0, "state": {"running": {}}}]},
}  # fmt: skip


def replicaset(revision: int, image: str, env_value: str, created: str, replicas: int) -> dict:
    return {
        "metadata": {
            "name": f"cartservice-rs{revision}",
            "creationTimestamp": created,
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
            "ownerReferences": [{"uid": "deploy-uid"}],
        },
        "spec": {
            "replicas": replicas,
            "template": {
                "metadata": {"labels": {"app": "cartservice", "pod-template-hash": f"h{revision}"}},
                "spec": {"containers": [
                    container(image=image, env=[{"name": "REDIS_ADDR", "value": env_value}])]},
            },
        },
        "status": {"readyReplicas": replicas},
    }  # fmt: skip


RESPONSES = {
    "/api/v1/namespaces/boutique/pods": {"items": [HEALTHY_POD, CRASHING_POD]},
    "/api/v1/namespaces/boutique/pods/cartservice-abc": CRASHING_POD,
    "/api/v1/namespaces/boutique/events": {"items": [
        {"metadata": {}, "lastTimestamp": "2026-11-10T11:00:00Z", "type": "Normal",
         "reason": "Pulled", "involvedObject": {"kind": "Pod", "name": "old-pod"},
         "message": "long ago"},
        {"metadata": {}, "lastTimestamp": "2026-11-10T13:05:00Z", "type": "Warning",
         "reason": "BackOff", "count": 12,
         "involvedObject": {"kind": "Pod", "name": "cartservice-abc"},
         "message": "Back-off restarting failed container server"},
        {"metadata": {}, "lastTimestamp": "2026-11-10T13:02:00Z", "type": "Normal",
         "reason": "ScalingReplicaSet",
         "involvedObject": {"kind": "Deployment", "name": "cartservice"},
         "message": "Scaled up replica set cartservice-rs2 from 0 to 1"},
    ]},
    "/apis/apps/v1/namespaces/boutique/deployments/cartservice": {
        "metadata": {"name": "cartservice", "uid": "deploy-uid",
                     "labels": {"app.kubernetes.io/name": "cartservice"},
                     "annotations": {"deployment.kubernetes.io/revision": "2"}},
        "spec": {"replicas": 1, "strategy": {"type": "RollingUpdate"},
                 "template": {"spec": {"containers": [container(image="repo/cartservice:v2")]}}},
        "status": {"readyReplicas": 0, "updatedReplicas": 1, "unavailableReplicas": 1,
                   "conditions": [{"type": "Available", "status": "False",
                                   "reason": "MinimumReplicasUnavailable",
                                   "message": "Deployment does not have minimum availability."}]},
    },
    "/apis/apps/v1/namespaces/boutique/replicasets": {"items": [
        replicaset(2, "repo/cartservice:v2", "redis-cart-v2:6379", "2026-11-10T13:02:00Z", 1),
        replicaset(1, "repo/cartservice:v1", "redis-cart:6379", "2026-11-09T09:00:00Z", 0),
        {"metadata": {"name": "other", "ownerReferences": [{"uid": "someone-else"}],
                      "annotations": {"deployment.kubernetes.io/revision": "9"}}},
    ]},
    "/api/v1/nodes": {"items": [
        {"metadata": {"name": "heimdall-worker2"}, "spec": {},
         "status": {"allocatable": {"cpu": "16", "memory": "32Gi"}, "conditions": [
             {"type": "Ready", "status": "Unknown", "reason": "NodeStatusUnknown",
              "lastHeartbeatTime": "2026-11-10T13:04:00Z"},
             {"type": "MemoryPressure", "status": "False"}]}},
        {"metadata": {"name": "heimdall-worker"}, "spec": {},
         "status": {"allocatable": {"cpu": "16", "memory": "32Gi"}, "conditions": [
             {"type": "Ready", "status": "True", "reason": "KubeletReady",
              "lastHeartbeatTime": "2026-11-10T13:09:50Z"},
             {"type": "DiskPressure", "status": "True"}]}},
    ]},
}  # fmt: skip


@pytest.fixture
def requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def tools(requests: list[httpx.Request]) -> k8s.KubernetesTools:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/log"):
            if request.url.params.get("previous") == "true":
                return httpx.Response(200, text="panic: out of memory\nexit\n")
            return httpx.Response(200, text="starting\nlistening on 7070\n")
        if path.endswith("/secrets"):
            return httpx.Response(403, json={"message": "secrets is forbidden"})
        if path not in RESPONSES:
            return httpx.Response(404, json={"message": f"{path} not found"})
        return httpx.Response(200, content=json.dumps(RESPONSES[path]))

    client = KubeClient(httpx.Client(transport=httpx.MockTransport(handler), base_url="https://k"))
    return k8s.KubernetesTools(client, {"boutique"}, clock=lambda: NOW)


def test_list_pods_shows_what_is_wrong_at_a_glance(tools: k8s.KubernetesTools) -> None:
    assert tools.list_pods(k8s.ListPodsArgs(namespace="boutique")).split("\n") == [
        "pod=adservice-xyz phase=Running ready=1/1 restarts=0 node=heimdall-worker2 age=2d",
        "pod=cartservice-abc phase=Running ready=0/1 restarts=5 node=heimdall-worker2 age=10m "
        "waiting=CrashLoopBackOff last_termination=OOMKilled(exit=137)",
    ]


def test_list_pods_passes_the_label_selector(
    tools: k8s.KubernetesTools, requests: list[httpx.Request]
) -> None:
    tools.list_pods(k8s.ListPodsArgs(namespace="boutique", label_selector="app=x"))
    assert requests[-1].url.params["labelSelector"] == "app=x"


def test_describe_pod_shows_env_names_but_never_values(tools: k8s.KubernetesTools) -> None:
    out = tools.describe_pod(k8s.PodArgs(namespace="boutique", name="cartservice-abc"))

    assert "env_names=REDIS_ADDR,API_TOKEN (values are never shown)" in out
    assert "super-secret-value" not in out
    assert "redis-cart:6379" not in out
    assert "probes readiness=grpc:7070 liveness=http:8080/healthz" in out
    assert "state=waiting reason=CrashLoopBackOff" in out
    assert "last_termination=OOMKilled(exit=137)" in out
    assert "requests(cpu=200m,memory=64Mi) limits(cpu=300m,memory=128Mi)" in out
    # Mesh sidecars and injected volumes are left out.
    assert "linkerd" not in out
    assert "volumes=settings(configMap)" in out
    assert "mounts=settings:/etc/settings" in out


def test_logs_default_to_the_application_container(
    tools: k8s.KubernetesTools, requests: list[httpx.Request]
) -> None:
    out = tools.get_pod_logs(k8s.LogsArgs(namespace="boutique", pod="cartservice-abc", tail=50))

    assert out == "starting\nlistening on 7070"
    assert requests[-1].url.params["container"] == "server"
    assert requests[-1].url.params["tailLines"] == "50"


def test_previous_logs_show_the_crash(tools: k8s.KubernetesTools) -> None:
    out = tools.get_pod_logs(
        k8s.LogsArgs(namespace="boutique", pod="cartservice-abc", previous=True)
    )
    assert out.startswith("panic: out of memory")


def test_events_are_filtered_by_time_and_object_and_sorted(tools: k8s.KubernetesTools) -> None:
    out = tools.get_events(k8s.EventsArgs(namespace="boutique", since_minutes=30)).split("\n")

    assert len(out) == 2
    assert out[0].startswith("2026-11-10T13:02:00Z type=Normal reason=ScalingReplicaSet")
    assert "object=Pod/cartservice-abc count=12" in out[1]

    only = tools.get_events(k8s.EventsArgs(namespace="boutique", involved_object="cartservice-abc"))
    assert only.count("\n") == 0 and "BackOff" in only


def test_deployment_summary(tools: k8s.KubernetesTools) -> None:
    out = tools.get_deployment(k8s.DeploymentArgs(namespace="boutique", name="cartservice"))

    assert out.startswith(
        "deployment=cartservice revision=2 replicas desired=1 ready=0 updated=1 available=0 "
        "unavailable=1"
    )
    assert "image=repo/cartservice:v2" in out
    assert "condition Available=False reason=MinimumReplicasUnavailable" in out


def test_rollout_history_lists_only_this_deployments_revisions(tools: k8s.KubernetesTools) -> None:
    out = tools.get_rollout_history(k8s.DeploymentArgs(namespace="boutique", name="cartservice"))

    assert out.split("\n") == [
        "revision=1 created=2026-11-09T09:00:00Z image=repo/cartservice:v1 replicas=0 ready=0",
        "revision=2 (current) created=2026-11-10T13:02:00Z image=repo/cartservice:v2 "
        "replicas=1 ready=1",
    ]


def test_diff_shows_the_change_and_masks_env_values(tools: k8s.KubernetesTools) -> None:
    out = tools.diff_revisions(
        k8s.DiffArgs(namespace="boutique", name="cartservice", from_revision=1, to_revision=2)
    )

    assert "~ spec.containers[server].image: repo/cartservice:v1 -> repo/cartservice:v2" in out
    assert "~ spec.containers[server].env[REDIS_ADDR].value: <masked> (value changed)" in out
    assert "redis-cart" not in out
    assert "pod-template-hash" not in out


def test_diff_of_unknown_revision_says_which_exist(tools: k8s.KubernetesTools) -> None:
    with pytest.raises(ToolError, match=r"revision 7 not found; available: \[1, 2\]"):
        tools.diff_revisions(
            k8s.DiffArgs(namespace="boutique", name="cartservice", from_revision=1, to_revision=7)
        )


def test_node_status_shows_unreachable_node_and_pressure(tools: k8s.KubernetesTools) -> None:
    worker, worker2 = tools.get_node_status(k8s.NoArgs()).split("\n")

    assert "node=heimdall-worker ready=True" in worker and "pressure=DiskPressure" in worker
    assert "node=heimdall-worker2 ready=Unknown reason=NodeStatusUnknown" in worker2
    assert "last_heartbeat=2026-11-10T13:04:00Z" in worker2


def test_other_namespaces_are_refused_without_calling_the_api(
    tools: k8s.KubernetesTools, requests: list[httpx.Request]
) -> None:
    with pytest.raises(ToolError, match="namespace 'kube-system' is not allowed"):
        tools.list_pods(k8s.ListPodsArgs(namespace="kube-system"))
    assert requests == []


def test_missing_object_is_a_tool_error(tools: k8s.KubernetesTools) -> None:
    with pytest.raises(ToolError, match="not found"):
        tools.describe_pod(k8s.PodArgs(namespace="boutique", name="ghost"))


def test_every_tool_is_registered_with_a_description(tools: k8s.KubernetesTools) -> None:
    names = [t.name for t in tools.tools()]

    assert names == ["list_pods", "describe_pod", "get_pod_logs", "get_events", "get_deployment",
                     "get_rollout_history", "diff_revisions", "get_node_status"]  # fmt: skip
    assert all(len(t.description) > 20 for t in tools.tools())
