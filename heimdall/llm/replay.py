"""Record and replay of LLM calls, so agent tests are deterministic and free.

A recording is keyed by a hash of everything that determines the answer:
model, prompt version, messages and response schema. If a prompt changes, the
key changes, and replay fails with a clear "fixture is stale" error instead of
silently answering a different question.
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import rfc8785

from heimdall.llm.base import LLM, Message, Response


class FixtureStale(Exception):
    pass


def request_key(
    model: str, prompt_version: str, messages: list[Message], schema: dict[str, Any] | None
) -> str:
    payload = {
        "model": model,
        "prompt_version": prompt_version,
        "messages": [m.model_dump() for m in messages],
        "schema": schema,
    }
    return hashlib.sha256(rfc8785.dumps(payload)).hexdigest()


class RecordingLLM:
    """Wraps a real provider and saves every exchange to a directory."""

    def __init__(self, inner: LLM, directory: Path, prompt_version: str) -> None:
        self._inner = inner
        self._dir = directory
        self._version = prompt_version
        self.model = inner.model

    def complete(
        self, messages: list[Message], response_schema: dict[str, Any] | None = None
    ) -> Response:
        response = self._inner.complete(messages, response_schema)
        key = request_key(self.model, self._version, messages, response_schema)
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{key}.json").write_text(json.dumps(asdict(response), indent=2) + "\n")
        return response


class ReplayLLM:
    """Answers from recordings only. Never calls a provider."""

    def __init__(self, directory: Path, model: str, prompt_version: str) -> None:
        self._dir = directory
        self._version = prompt_version
        self.model = model

    def complete(
        self, messages: list[Message], response_schema: dict[str, Any] | None = None
    ) -> Response:
        key = request_key(self.model, self._version, messages, response_schema)
        path = self._dir / f"{key}.json"
        if not path.exists():
            raise FixtureStale(
                f"no recording for this request (key {key[:12]}). The prompt, tools or model "
                "changed since the fixtures were recorded; record them again."
            )
        return Response(**json.loads(path.read_text()))
