"""The tool registry: the only way the model touches the outside world.

Every call is validated, audited, time-limited, redacted and sanitized here,
so individual tools stay simple and cannot forget a step.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from heimdall.core.audit import AuditLog, EventType
from heimdall.tools import sanitize
from heimdall.tools.redact import redact


class ToolError(Exception):
    """A failure the model should see and can react to (bad arguments, not
    found, forbidden namespace). Anything else is a bug and is reported to
    the model only as a generic failure."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments: type[BaseModel]
    handler: Callable[[Any], str]
    timeout_seconds: float = 20
    output_limit: int = sanitize.DEFAULT_LIMIT

    def schema(self) -> dict[str, Any]:
        """The definition shown to the model."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.arguments.model_json_schema(),
        }


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool: str
    arguments: dict[str, Any]
    # Redacted, complete. Stored in the audit log; evidence quotes are checked
    # against this.
    output: str
    # What the model sees: truncated, flagged, wrapped as untrusted data.
    for_model: str
    ok: bool
    flagged_lines: int = 0


class ToolRegistry:
    def __init__(self, audit: AuditLog) -> None:
        self._audit = audit
        self._tools: dict[str, Tool] = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name} is already registered")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def _next_call_id(self, incident_id: str) -> str:
        calls = sum(
            1 for e in self._audit.events(incident_id) if e.event_type == EventType.TOOL_CALL
        )
        return f"tc_{calls + 1}"

    def call(self, incident_id: str, name: str, arguments: dict[str, Any]) -> ToolResult:
        call_id = self._next_call_id(incident_id)
        self._audit.append(
            EventType.TOOL_CALL,
            {"call_id": call_id, "tool": name, "arguments": arguments},
            actor="investigator",
            incident_id=incident_id,
        )

        ok, raw = self._run(name, arguments)
        output = redact(raw)
        limit = self._tools[name].output_limit if name in self._tools else sanitize.DEFAULT_LIMIT
        text, flagged = sanitize.sanitize(output, limit)

        self._audit.append(
            EventType.TOOL_RESULT,
            {"call_id": call_id, "tool": name, "ok": ok, "output": output,
             "flagged_lines": flagged},
            actor="investigator",
            incident_id=incident_id,
        )  # fmt: skip
        return ToolResult(
            call_id=call_id,
            tool=name,
            arguments=arguments,
            output=output,
            for_model=sanitize.wrap(name, call_id, text),
            ok=ok,
            flagged_lines=flagged,
        )

    def _run(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        tool = self._tools.get(name)
        if tool is None:
            return False, f"error: unknown tool {name!r}. Available: {', '.join(self._tools)}"
        try:
            parsed = tool.arguments.model_validate(arguments)
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
            )
            return False, f"error: invalid arguments: {problems}"

        future = self._pool.submit(tool.handler, parsed)
        try:
            return True, future.result(timeout=tool.timeout_seconds)
        except FutureTimeout:
            future.cancel()
            return False, f"error: {name} timed out after {tool.timeout_seconds:g}s"
        except ToolError as e:
            return False, f"error: {e}"
        except Exception as e:  # noqa: BLE001 - a tool bug must not end the investigation
            return False, f"error: {name} failed ({type(e).__name__})"
