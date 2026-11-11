"""The static service dependency graph."""

from pathlib import Path

import yaml


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
