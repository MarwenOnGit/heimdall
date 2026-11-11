"""Ollama provider: a model running on the same machine, no cloud involved."""

import json
import time
from typing import Any

import httpx

from heimdall.llm.base import LLMError, Message, Response


class OllamaLLM:
    def __init__(
        self,
        model: str,
        client: httpx.Client | None = None,
        *,
        temperature: float = 0,
        context_tokens: int = 16384,
        max_tokens: int = 2000,
    ) -> None:
        self.model = model
        self._client = client or httpx.Client(base_url="http://127.0.0.1:11434", timeout=300)
        self._options = {
            "temperature": temperature,
            "num_ctx": context_tokens,
            "num_predict": max_tokens,
            "seed": 0,
        }

    def complete(
        self, messages: list[Message], response_schema: dict[str, Any] | None = None
    ) -> Response:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "think": False,
            "options": self._options,
        }
        if response_schema is not None:
            body["format"] = response_schema
        started = time.monotonic()
        try:
            response = self._client.post("/api/chat", json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            raise LLMError(f"Ollama request failed: {e}") from e
        return Response(
            content=data.get("message", {}).get("content", ""),
            model=self.model,
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            latency_seconds=round(time.monotonic() - started, 3),
        )
