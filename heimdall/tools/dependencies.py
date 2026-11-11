"""The static service dependency graph."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from heimdall.tools.registry import Tool, ToolError


class DependencyGraph:
    def __init__(self, depends_on: dict[str, list[str]]) -> None:
        self._upstream: dict[str, set[str]] = {s: set(d) for s, d in depends_on.items()}
        self._downstream: dict[str, set[str]] = {s: set() for s in depends_on}
        for service, deps in depends_on.items():
            for dep in deps:
                if dep not in depends_on:
                    raise ValueError(f"{service} depends on unknown service {dep}")
                self._downstream[dep].add(service)

    @classmethod
    def load(cls, path: Path) -> "DependencyGraph":
        raw = yaml.safe_load(path.read_text()) or {}
        return cls({service: list(deps or []) for service, deps in raw.items()})

    @property
    def services(self) -> set[str]:
        return set(self._upstream)

    def upstream(self, service: str) -> set[str]:
        """Services that `service` calls."""
        return set(self._upstream.get(service, set()))

    def downstream(self, service: str) -> set[str]:
        """Services that call `service`."""
        return set(self._downstream.get(service, set()))

    def connected(self, a: str, b: str) -> bool:
        """True when the two services are the same or call each other directly."""
        return a == b or b in self._upstream.get(a, set()) or a in self._upstream.get(b, set())


class ServiceArgs(BaseModel):
    service: str = Field(description="Service name, e.g. cartservice")


class DependencyTools:
    def __init__(self, graph: DependencyGraph) -> None:
        self._graph = graph

    def get_service_dependencies(self, args: ServiceArgs) -> str:
        if args.service not in self._graph.services:
            raise ToolError(
                f"unknown service {args.service!r}; known: {sorted(self._graph.services)}"
            )
        calls = sorted(self._graph.upstream(args.service))
        called_by = sorted(self._graph.downstream(args.service))
        return (
            f"service={args.service}\n"
            f"depends_on={','.join(calls) or 'nothing'}\n"
            f"used_by={','.join(called_by) or 'nothing'}"
        )

    def tools(self) -> list[Tool]:
        return [
            Tool("get_service_dependencies", "Show which services a service depends on and "
                 "which services use it. A failing dependency often shows up as errors in the "
                 "services that use it.", ServiceArgs, self.get_service_dependencies),
        ]  # fmt: skip
