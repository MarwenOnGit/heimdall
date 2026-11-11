"""Runtime configuration, read from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    alert_token: str
    db_path: Path
    prometheus_url: str
    dependencies_path: Path
    audit_key_path: Path | None
    anchor_file: Path | None
    anchor_every_events: int
    gitea_url: str | None
    gitea_user: str | None
    gitea_token: str | None
    gitops_repo: str
    loki_url: str
    kube_url: str
    kube_token_file: Path
    kube_ca_file: Path | None
    namespaces: frozenset[str]
    # "off" disables the investigator: alerts are only grouped into incidents.
    # Otherwise "ollama" (local) or "anthropic" (API).
    llm_provider: str
    llm_model: str
    ollama_url: str
    llm_context_tokens: int
    settle_seconds: float
    max_tool_calls: int
    max_investigation_seconds: float
    # observe: diagnose only.
    # dry_run: also plan, check policy, and send each allowed action to the
    #   API server as a dry run. Nothing changes.
    # live: run allowed actions for real, verify each, undo what does not help.
    mode: str
    approval_secret: str
    webhook_url: str | None
    public_url: str
    verify_timeout_seconds: float
    actor_token_file: Path | None
    opa_url: str
    # Off for the "without search" benchmark comparison.
    search_past_incidents: bool = True
    # Anthropic provider only. Effort: low, medium, high, xhigh or max.
    llm_effort: str | None = None
    llm_fallbacks: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        def path(name: str) -> Path | None:
            value = os.environ.get(name)
            return Path(value) if value else None

        provider = os.environ.get("HEIMDALL_LLM_PROVIDER", "off")
        return cls(
            alert_token=os.environ.get("HEIMDALL_ALERT_TOKEN", ""),
            db_path=Path(os.environ.get("HEIMDALL_DB", "heimdall.sqlite")),
            prometheus_url=os.environ.get(
                "HEIMDALL_PROMETHEUS_URL", "http://kps-prometheus.monitoring.svc:9090"
            ),
            dependencies_path=Path(
                os.environ.get("HEIMDALL_DEPENDENCIES", "docs/dependencies.yaml")
            ),
            audit_key_path=path("HEIMDALL_AUDIT_KEY"),
            anchor_file=path("HEIMDALL_ANCHOR_FILE"),
            anchor_every_events=int(os.environ.get("HEIMDALL_ANCHOR_EVERY", "50")),
            gitea_url=os.environ.get("HEIMDALL_GITEA_URL"),
            gitea_user=os.environ.get("HEIMDALL_GITEA_USER"),
            gitea_token=os.environ.get("HEIMDALL_GITEA_TOKEN"),
            gitops_repo=os.environ.get("HEIMDALL_GITOPS_REPO", "heimdall/gitops"),
            loki_url=os.environ.get("HEIMDALL_LOKI_URL", "http://loki.monitoring.svc:3100"),
            kube_url=os.environ.get("HEIMDALL_KUBE_URL", "https://kubernetes.default.svc"),
            kube_token_file=Path(
                os.environ.get(
                    "HEIMDALL_KUBE_TOKEN_FILE",
                    "/var/run/secrets/kubernetes.io/serviceaccount/token",
                )
            ),
            kube_ca_file=path("HEIMDALL_KUBE_CA_FILE")
            or Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"),
            namespaces=frozenset(
                n for n in os.environ.get("HEIMDALL_NAMESPACES", "boutique").split(",") if n
            ),
            llm_provider=provider,
            llm_model=os.environ.get("HEIMDALL_LLM_MODEL")
            or ("claude-opus-5-5" if provider == "anthropic" else "qwen3:8b"),
            ollama_url=os.environ.get("HEIMDALL_OLLAMA_URL", "http://127.0.0.1:11434"),
            llm_context_tokens=int(os.environ.get("HEIMDALL_LLM_CONTEXT_TOKENS", "16384")),
            settle_seconds=float(os.environ.get("HEIMDALL_SETTLE_SECONDS", "45")),
            max_tool_calls=int(os.environ.get("HEIMDALL_MAX_TOOL_CALLS", "15")),
            max_investigation_seconds=float(
                os.environ.get("HEIMDALL_MAX_INVESTIGATION_SECONDS", "240")
            ),
            mode=os.environ.get("HEIMDALL_MODE", "observe"),
            approval_secret=os.environ.get("HEIMDALL_APPROVAL_SECRET", ""),
            webhook_url=os.environ.get("HEIMDALL_WEBHOOK") or None,
            public_url=os.environ.get("HEIMDALL_PUBLIC_URL", "http://localhost:8000"),
            verify_timeout_seconds=float(os.environ.get("HEIMDALL_VERIFY_TIMEOUT_SECONDS", "300")),
            actor_token_file=path("HEIMDALL_ACTOR_TOKEN_FILE"),
            opa_url=os.environ.get("HEIMDALL_OPA_URL", "http://127.0.0.1:8181"),
            search_past_incidents=os.environ.get("HEIMDALL_SEARCH_PAST_INCIDENTS", "true")
            != "false",
            llm_effort=os.environ.get("HEIMDALL_LLM_EFFORT") or None,
            llm_fallbacks=os.environ.get("HEIMDALL_LLM_FALLBACKS", "true") != "false",
        )
