"""Metrics tool: free-form PromQL, confined to the allowed namespaces.

The query is parsed, not pattern-matched: every series selector in it must pin
`namespace` to an allowed value with an equality matcher. That holds however
the query is nested, so the model cannot wander into other namespaces (or be
talked into it by something it read in a log).
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import Any

import httpx
import promql_parser
from pydantic import BaseModel, Field

from heimdall.tools.registry import Tool, ToolError

MAX_SERIES = 20


def check_namespaces(query: str, allowed: set[str]) -> None:
    """Raise ToolError unless every selector is confined to an allowed namespace."""
    try:
        expr = promql_parser.parse(query)
    except Exception as e:  # noqa: BLE001 - the parser raises plain ValueError-likes
        raise ToolError(f"invalid PromQL: {e}") from e

    problems: list[str] = []
    selectors = 0

    def visit(node: Any) -> bool:
        nonlocal selectors
        if isinstance(node, promql_parser.VectorSelector):
            selectors += 1
            name = node.name or "{...}"
            if node.matchers.or_matchers:
                problems.append(f"{name}: 'or' matchers are not allowed")
                return True
            pinned = [
                m
                for m in node.matchers.matchers
                if m.name == "namespace" and m.op == promql_parser.MatchOp.Equal
            ]
            if not pinned:
                problems.append(f'{name}: add namespace="<allowed namespace>"')
            elif any(m.value not in allowed for m in pinned):
                problems.append(f"{name}: namespace must be one of {sorted(allowed)}")
        return True

    promql_parser.walk(expr, visit)
    if selectors == 0:
        raise ToolError("the query selects no series")
    if problems:
        raise ToolError("query rejected: " + "; ".join(problems))


def summarize(values: list[tuple[float, float]]) -> str:
    """Describe one series: range, trend, and the moment it changed, if any."""
    points = [v for _, v in values]
    first, last = points[0], points[-1]
    text = f"first={first:.4g} last={last:.4g} min={min(points):.4g} max={max(points):.4g}"

    spread = max(points) - min(points)
    scale = max(abs(fmean(points)), 1e-9)
    if spread / scale < 0.05:
        return text + " trend=flat"
    text += " trend=" + ("rising" if last > first else "falling" if last < first else "mixed")

    # Single change point: the split that best separates "before" from "after".
    best, best_gap = None, 0.0
    for i in range(2, len(points) - 1):
        gap = abs(fmean(points[i:]) - fmean(points[:i]))
        if gap > best_gap:
            best, best_gap = i, gap
    if best is not None and best_gap > 0.5 * spread:
        at = datetime.fromtimestamp(values[best][0], UTC)
        before, after = fmean(points[:best]), fmean(points[best:])
        text += f" change_point={at:%H:%M:%S}Z (mean {before:.4g} -> {after:.4g})"
    return text


class MetricsArgs(BaseModel):
    promql: str = Field(
        description='PromQL. Every selector must include namespace="..." for an allowed namespace.'
    )
    range_minutes: int = Field(default=30, ge=1, le=720)
    step_seconds: int = Field(default=30, ge=15, le=600)


class PrometheusTools:
    def __init__(
        self,
        client: httpx.Client,
        allowed_namespaces: set[str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._allowed = allowed_namespaces
        self._clock = clock

    def query_metrics(self, args: MetricsArgs) -> str:
        check_namespaces(args.promql, self._allowed)
        end = self._clock()
        start = end - timedelta(minutes=args.range_minutes)
        try:
            response = self._client.get(
                "/api/v1/query_range",
                params={
                    "query": args.promql,
                    "start": start.timestamp(),
                    "end": end.timestamp(),
                    "step": args.step_seconds,
                },
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ToolError("Prometheus is unreachable") from e
        if body.get("status") != "success":
            raise ToolError(f"Prometheus rejected the query: {body.get('error', 'unknown error')}")

        series = body["data"]["result"]
        if not series:
            return "no data for this query in the selected range"
        lines = [f"range={start:%H:%M:%S}Z..{end:%H:%M:%S}Z series={len(series)}"]
        for item in series[:MAX_SERIES]:
            labels = ",".join(
                f'{k}="{v}"' for k, v in sorted(item["metric"].items()) if k != "__name__"
            )
            name = item["metric"].get("__name__", "")
            values = [(float(t), float(v)) for t, v in item["values"] if v not in ("NaN", "+Inf")]
            if values:
                lines.append(f"{name}{{{labels}}} {summarize(values)}")
        if len(series) > MAX_SERIES:
            lines.append(f"[{len(series) - MAX_SERIES} more series omitted; aggregate the query]")
        return "\n".join(lines)

    def tools(self) -> list[Tool]:
        return [
            Tool("query_metrics", "Run a PromQL range query and get a summary of each series: "
                 "first, last, min, max, trend, and the time it changed. Useful series: "
                 "heimdall:service_success_rate:ratio_rate2m and "
                 "heimdall:service_latency_ms:p95_rate5m (per service), "
                 "kube_pod_container_status_restarts_total, container_memory_working_set_bytes, "
                 "kube_deployment_status_replicas_available.", MetricsArgs, self.query_metrics),
        ]  # fmt: skip
