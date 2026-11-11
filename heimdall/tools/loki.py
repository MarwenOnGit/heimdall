"""Log search tool: free-form LogQL, confined to the allowed namespaces."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, Field

from heimdall.tools.registry import Tool, ToolError

_MATCHER = re.compile(r'\s*([A-Za-z_]\w*)\s*(=~|!~|!=|=)\s*("(?:[^"\\]|\\.)*"|`[^`]*`)\s*(,|$)')


def stream_selectors(query: str) -> list[str]:
    """Return the contents of every `{...}` stream selector in a LogQL query.

    Braces inside quoted strings (for example a `line_format` template) are
    not selectors, so the query is scanned with string awareness.
    """
    selectors: list[str] = []
    quote: str | None = None
    start: int | None = None
    i = 0
    while i < len(query):
        char = query[i]
        if quote:
            if char == "\\" and quote == '"':
                i += 1
            elif char == quote:
                quote = None
        elif char in '"`':
            quote = char
        elif char == "{" and start is None:
            start = i + 1
        elif char == "}" and start is not None:
            selectors.append(query[start:i])
            start = None
        i += 1
    if quote or start is not None:
        raise ToolError("invalid LogQL: unbalanced quote or brace")
    return selectors


def check_namespaces(query: str, allowed: set[str]) -> None:
    """Raise ToolError unless every stream selector pins an allowed namespace."""
    selectors = stream_selectors(query)
    if not selectors:
        raise ToolError('the query needs a stream selector such as {namespace="..."}')
    for selector in selectors:
        position, namespaces = 0, []
        while position < len(selector):
            match = _MATCHER.match(selector, position)
            if not match:
                raise ToolError(f"invalid stream selector {{{selector}}}")
            name, op, value = match[1], match[2], match[3][1:-1]
            if name == "namespace":
                if op != "=":
                    raise ToolError('namespace must be matched with "=", not a regex or negation')
                namespaces.append(value)
            position = match.end()
        if not namespaces:
            raise ToolError(f'selector {{{selector}}} needs namespace="<allowed namespace>"')
        if any(ns not in allowed for ns in namespaces):
            raise ToolError(f"namespace must be one of {sorted(allowed)}")


class LogsQueryArgs(BaseModel):
    logql: str = Field(
        description='LogQL, e.g. {namespace="boutique", service="cartservice"} |= "error"'
    )
    since_minutes: int = Field(default=15, ge=1, le=720)
    limit: int = Field(default=200, ge=1, le=1000)


class LokiTools:
    def __init__(
        self,
        client: httpx.Client,
        allowed_namespaces: set[str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._allowed = allowed_namespaces
        self._clock = clock

    def query_logs(self, args: LogsQueryArgs) -> str:
        check_namespaces(args.logql, self._allowed)
        end = self._clock()
        start = end - timedelta(minutes=args.since_minutes)
        try:
            response = self._client.get(
                "/loki/api/v1/query_range",
                params={
                    "query": args.logql,
                    "start": int(start.timestamp() * 1e9),
                    "end": int(end.timestamp() * 1e9),
                    "limit": args.limit,
                    "direction": "backward",
                },
            )
        except httpx.HTTPError as e:
            raise ToolError("Loki is unreachable") from e
        if response.status_code == 400:
            raise ToolError(f"Loki rejected the query: {response.text.strip()[:300]}")
        if not response.is_success:
            raise ToolError(f"Loki returned {response.status_code}")

        entries: list[tuple[int, str, str]] = []
        for stream in response.json()["data"]["result"]:
            pod = stream.get("stream", {}).get("pod", "-")
            for timestamp, line in stream.get("values", []):
                entries.append((int(timestamp), pod, line))
        if not entries:
            return "no log lines matched"
        entries.sort()
        return "\n".join(
            f"{datetime.fromtimestamp(ts / 1e9, UTC):%H:%M:%S}Z pod={pod} {line}"
            for ts, pod, line in entries
        )

    def tools(self) -> list[Tool]:
        return [
            Tool("query_logs", "Search logs across pods with LogQL. Labels: namespace, service, "
                 "pod, container. Lines come back oldest first with their pod.",
                 LogsQueryArgs, self.query_logs),
        ]  # fmt: skip
