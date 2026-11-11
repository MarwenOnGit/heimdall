from datetime import UTC, datetime

import httpx
import pytest

from heimdall.tools import loki, prometheus
from heimdall.tools.registry import ToolError

ALLOWED = {"boutique"}
NOW = datetime(2026, 11, 10, 13, 30, 0, tzinfo=UTC)

# ------------------------------------------------------------------ PromQL


@pytest.mark.parametrize(
    "query",
    [
        'up{namespace="boutique"}',
        'heimdall:service_success_rate:ratio_rate2m{namespace="boutique", service="frontend"}',
        'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="boutique"}[5m]))',
        'rate(a{namespace="boutique"}[2m]) / on (pod) rate(b{namespace="boutique"}[2m]) > 0.5',
        'histogram_quantile(0.95, sum by (le) (rate(x_bucket{namespace="boutique"}[5m])))',
        'max_over_time(kube_pod_container_status_restarts_total{namespace="boutique"}[1h:1m])',
    ],
)
def test_queries_confined_to_the_namespace_are_accepted(query: str) -> None:
    prometheus.check_namespaces(query, ALLOWED)


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("up", "add namespace"),
        ('up{namespace="kube-system"}', "must be one of"),
        ('up{namespace=~"boutique|kube-system"}', "add namespace"),
        ('up{namespace!="x"}', "add namespace"),
        ('up{namespace="boutique"} + on() group_left kube_secret_info', "kube_secret_info"),
        ('sum(rate(a{namespace="boutique"}[5m])) / sum(rate(b[5m]))', "b: add namespace"),
        ('up{namespace="boutique"} or up{namespace="monitoring"}', "must be one of"),
        ('{__name__=~".+"}', "add namespace"),
        ('up{namespace="boutique" or namespace="monitoring"}', "not allowed"),
        ("1 + 1", "selects no series"),
        ("up{", "invalid PromQL"),
    ],
)
def test_queries_reaching_outside_are_rejected(query: str, reason: str) -> None:
    with pytest.raises(ToolError, match=reason):
        prometheus.check_namespaces(query, ALLOWED)


def series(values: list[float], start: float = NOW.timestamp() - 1800) -> list[list]:
    return [[start + 30 * i, str(v)] for i, v in enumerate(values)]


def prometheus_tools(result: list, calls: list | None = None) -> prometheus.PrometheusTools:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://prom")
    return prometheus.PrometheusTools(client, ALLOWED, clock=lambda: NOW)


def test_step_change_is_reported_with_its_time() -> None:
    values = [0.998] * 30 + [0.69] * 30
    tools = prometheus_tools(
        [{"metric": {"__name__": "success_rate", "service": "frontend"}, "values": series(values)}]
    )

    out = tools.query_metrics(prometheus.MetricsArgs(promql='success_rate{namespace="boutique"}'))

    line = out.split("\n")[1]
    assert line.startswith('success_rate{service="frontend"} first=0.998 last=0.69 min=0.69')
    assert "trend=falling" in line
    assert "change_point=13:15:00Z (mean 0.998 -> 0.69)" in line


def test_flat_series_has_no_change_point() -> None:
    tools = prometheus_tools([{"metric": {"service": "adservice"}, "values": series([1.0] * 60)}])

    out = tools.query_metrics(prometheus.MetricsArgs(promql='x{namespace="boutique"}'))

    assert out.split("\n")[1].endswith("trend=flat")


def test_empty_result_and_too_many_series() -> None:
    args = prometheus.MetricsArgs(promql='x{namespace="boutique"}')
    assert prometheus_tools([]).query_metrics(args).startswith("no data")

    many = [{"metric": {"pod": f"p{i}"}, "values": series([1, 2, 3])} for i in range(25)]
    out = prometheus_tools(many).query_metrics(args)
    assert "[5 more series omitted; aggregate the query]" in out


def test_rejected_query_never_reaches_prometheus() -> None:
    calls: list = []
    with pytest.raises(ToolError):
        prometheus_tools([], calls).query_metrics(prometheus.MetricsArgs(promql="up"))
    assert calls == []


# -------------------------------------------------------------------- LogQL


@pytest.mark.parametrize(
    "query",
    [
        '{namespace="boutique"}',
        '{namespace="boutique", service="cartservice"} |= "error"',
        '{service=~"cart.*", namespace="boutique"} | json | line_format "{{.message}} }"',
        'sum(count_over_time({namespace="boutique"} |~ "panic|fatal" [5m]))',
        "{namespace=`boutique`} != `debug`",
    ],
)
def test_logql_confined_to_the_namespace_is_accepted(query: str) -> None:
    loki.check_namespaces(query, ALLOWED)


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ('{service="cartservice"}', "needs namespace"),
        ('{namespace="kube-system"}', "must be one of"),
        ('{namespace=~".+"}', 'matched with "="'),
        ('{namespace!="x"}', 'matched with "="'),
        ('{namespace="boutique"} or {namespace="monitoring"}', "must be one of"),
        ('sum(rate({namespace="boutique"}[1m])) / sum(rate({pod=~".+"}[1m]))', "needs namespace"),
        ('{namespace="boutique", namespace="kube-system"}', "must be one of"),
        ('|= "error"', "needs a stream selector"),
        ('{namespace="boutique"', "unbalanced"),
        ('{namespace="boutique" | x}', "invalid stream selector"),
    ],
)
def test_logql_reaching_outside_is_rejected(query: str, reason: str) -> None:
    with pytest.raises(ToolError, match=reason):
        loki.check_namespaces(query, ALLOWED)


def test_log_lines_come_back_oldest_first_with_their_pod() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        base = int(NOW.timestamp() * 1e9)
        return httpx.Response(200, json={"data": {"result": [
            {"stream": {"pod": "cart-b"}, "values": [[str(base - 1_000_000_000), "second"]]},
            {"stream": {"pod": "cart-a"}, "values": [[str(base - 5_000_000_000), "first"],
                                                     [str(base), "third"]]},
        ]}})  # fmt: skip

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://loki")
    tools = loki.LokiTools(client, ALLOWED, clock=lambda: NOW)

    out = tools.query_logs(loki.LogsQueryArgs(logql='{namespace="boutique"}', limit=50))

    assert out.split("\n") == [
        "13:29:55Z pod=cart-a first",
        "13:29:59Z pod=cart-b second",
        "13:30:00Z pod=cart-a third",
    ]
    assert calls[0].url.params["limit"] == "50"
    assert calls[0].url.params["direction"] == "backward"
