"""Builds the investigator and its tools from configuration."""

from collections.abc import Callable
from typing import Any

import httpx

from heimdall.agent.investigator import Budget, Investigator
from heimdall.agent.pipeline import Pipeline
from heimdall.agent.planner import Planner
from heimdall.config import Config
from heimdall.core.actions import ActionStore
from heimdall.core.audit import AuditLog
from heimdall.core.incidents import IncidentManager
from heimdall.core.models import Incident, ResourceRef
from heimdall.executor.executor import Executor
from heimdall.llm.base import LLM
from heimdall.llm.ollama import OllamaLLM
from heimdall.policy.gate import OpaHttpGate
from heimdall.policy.input_builder import InputBuilder
from heimdall.tools.dependencies import DependencyGraph, DependencyTools
from heimdall.tools.git import GitTools
from heimdall.tools.k8s import KubernetesTools
from heimdall.tools.kube import KubeClient, KubeError
from heimdall.tools.loki import LokiTools
from heimdall.tools.prometheus import PrometheusTools
from heimdall.tools.registry import Tool, ToolRegistry

# Kinds whose existence can be checked with the read-only identity.
_PATHS = {
    "Deployment": "/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
    "Pod": "/api/v1/namespaces/{namespace}/pods/{name}",
    "Service": "/api/v1/namespaces/{namespace}/services/{name}",
    "Node": "/api/v1/nodes/{name}",
}


def build_llm(config: Config) -> LLM | None:
    if config.llm_provider == "off":
        return None
    if config.llm_provider == "ollama":
        # Ollama only answers requests addressed to localhost. When it is
        # reached through the cluster bridge, the Host header must say so.
        client = httpx.Client(
            base_url=config.ollama_url, timeout=600, headers={"Host": "localhost"}
        )
        return OllamaLLM(config.llm_model, client, context_tokens=config.llm_context_tokens)
    raise ValueError(f"unknown LLM provider {config.llm_provider!r}")


def read_only_kube(config: Config) -> KubeClient:
    """The investigator's Kubernetes identity. It can read, never write."""
    return KubeClient.connect(
        config.kube_url, config.kube_token_file.read_text().strip(), config.kube_ca_file or True
    )


FirstLook = Callable[[Incident], list[tuple[str, dict[str, Any]]]]


def first_look(graph: DependencyGraph, namespaces: frozenset[str]) -> FirstLook:
    """The observations every investigation starts with, gathered without the
    model: what is running, what happened, what changed, and how the alerting
    services and everything around them are doing."""

    def collect(incident: Incident) -> list[tuple[str, dict[str, Any]]]:
        namespace = incident.namespace or sorted(namespaces)[0]
        calls: list[tuple[str, dict[str, Any]]] = []
        if incident.namespace is None:
            calls.append(("get_node_status", {}))
        calls += [
            ("list_pods", {"namespace": namespace}),
            ("get_events", {"namespace": namespace, "since_minutes": 30}),
            ("get_recent_changes", {"since_minutes": 30}),
        ]
        calls += [
            ("get_service_dependencies", {"service": service})
            for service in incident.services
            if service in graph.services
        ]
        success_rate = f'heimdall:service_success_rate:ratio_rate2m{{namespace="{namespace}"}}'
        calls.append(
            ("query_metrics", {"promql": success_rate, "range_minutes": 20, "step_seconds": 30})
        )
        return calls

    return collect


def build_pipeline(
    config: Config, audit: AuditLog, llm: LLM, manager: IncidentManager, actions: ActionStore
) -> Pipeline | None:
    """The remediation pipeline for the configured mode, or None in observe mode."""
    if config.mode == "observe":
        return None
    if config.mode != "dry_run":
        raise ValueError(
            f"mode {config.mode!r} is not supported: only observe and dry_run exist so far"
        )
    if config.actor_token_file is None:
        raise ValueError("dry_run mode needs HEIMDALL_ACTOR_TOKEN_FILE")
    reader = read_only_kube(config)
    # A second client with a different identity. It shares nothing with the
    # read-only one.
    actor = KubeClient.connect(
        config.kube_url, config.actor_token_file.read_text().strip(), config.kube_ca_file or True
    )
    return Pipeline(
        Planner(llm, audit),
        InputBuilder(reader, actions, mode=config.mode),
        OpaHttpGate(httpx.Client(base_url=config.opa_url, timeout=5)),
        Executor(actor, reader, actions, audit, dry_run=True),
        manager,
        audit,
    )


def build_investigator(config: Config, audit: AuditLog, llm: LLM) -> Investigator:
    kube = read_only_kube(config)
    namespaces = set(config.namespaces)
    graph = DependencyGraph.load(config.dependencies_path)

    prometheus = httpx.Client(base_url=config.prometheus_url, timeout=20)
    loki = httpx.Client(base_url=config.loki_url, timeout=20)
    tools: list[Tool] = [
        *KubernetesTools(kube, namespaces).tools(),
        *PrometheusTools(prometheus, namespaces).tools(),
        *LokiTools(loki, namespaces).tools(),
        *DependencyTools(graph).tools(),
    ]
    if config.gitea_url:
        git_client = httpx.Client(base_url=config.gitea_url, timeout=15)
        tools += GitTools(git_client, config.gitops_repo).tools()
    registry = ToolRegistry(audit)
    for tool in tools:
        registry.register(tool)

    def resource_exists(ref: ResourceRef) -> bool:
        template = _PATHS.get(ref.kind)
        if template is None:
            # Kinds the read-only identity cannot fetch (or that are missing
            # by definition, like a deleted ConfigMap) are not checked.
            return True
        try:
            kube.get(template.format(namespace=ref.namespace, name=ref.name))
        except KubeError as e:
            return e.status != 404
        return True

    return Investigator(
        llm,
        registry,
        audit,
        budget=Budget(
            max_tool_calls=config.max_tool_calls, max_seconds=config.max_investigation_seconds
        ),
        first_look=first_look(graph, config.namespaces),
        resource_exists=resource_exists,
    )
