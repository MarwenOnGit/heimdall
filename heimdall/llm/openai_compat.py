"""Any provider that speaks the OpenAI chat-completions protocol.

Gemini, OpenRouter, Groq, Mistral, Cerebras and most hosted open-weight
models do. The provider is chosen by its base URL and model name; the key
comes from the environment and is only ever sent as the Authorization header.

As with the Anthropic provider, what leaves the machine is tool output after
redaction and sanitization, and the reply is validated by the caller.

Free tiers are rate limited, so two things are built in: a minimum interval
between requests, and waiting when the provider answers 429 or 503. The wait
is bounded; a provider that stays busy becomes an `LLMError`, which the
investigator turns into an `unknown` diagnosis.
"""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from heimdall.llm.base import LLMError, Message, Response, extract_json, schema_instruction

_RETRYABLE = {429, 500, 502, 503, 504}


def _retry_after(response: httpx.Response, attempt: int, longest: float) -> float:
    """Seconds to wait before the next try: what the provider asks for, or a
    doubling delay when it does not say."""
    header = response.headers.get("retry-after", "")
    try:
        wanted = float(header)
    except ValueError:
        wanted = 2.0 * 2**attempt
    return max(0.0, min(wanted, longest))


class OpenAICompatLLM:
    def __init__(
        self,
        model: str,
        client: httpx.Client,
        *,
        max_tokens: int = 4000,
        temperature: float | None = 0,
        json_mode: bool = True,
        effort: str | None = None,
        min_interval_seconds: float = 0,
        max_retries: int = 4,
        max_wait_seconds: float = 60,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self._client = client
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._json_mode = json_mode
        self._effort = effort
        self._min_interval = min_interval_seconds
        self._max_retries = max_retries
        self._max_wait = max_wait_seconds
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    @classmethod
    def connect(cls, model: str, base_url: str, api_key: str, **options: Any) -> "OpenAICompatLLM":
        client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120,
        )
        return cls(model, client, **options)

    def _pace(self) -> None:
        """Keep requests at least `min_interval_seconds` apart."""
        if self._last_request is not None:
            wait = self._min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            self._pace()
            try:
                response = self._client.post("/chat/completions", json=body)
            except httpx.HTTPError as e:
                raise LLMError(f"LLM API could not be reached: {e}") from e
            if response.status_code in _RETRYABLE and attempt < self._max_retries:
                self._sleep(_retry_after(response, attempt, self._max_wait))
                continue
            if response.status_code >= 400:
                # The body says why (quota, unknown model, bad request). It
                # never contains the key.
                raise LLMError(f"LLM API returned {response.status_code}: {response.text[:300]}")
            try:
                data: dict[str, Any] = response.json()
            except json.JSONDecodeError as e:
                raise LLMError(f"LLM API reply was not JSON: {e}") from e
            return data
        raise LLMError("LLM API retries exhausted")  # pragma: no cover - loop always returns

    def complete(
        self, messages: list[Message], response_schema: dict[str, Any] | None = None
    ) -> Response:
        # One system message: not every compatible API accepts several.
        system = [m.content for m in messages if m.role == "system"]
        if response_schema is not None:
            # The schema goes in as an instruction: strict schema modes differ
            # between providers and reject parts of Heimdall's schemas.
            system.append(schema_instruction(response_schema))
        sent = [{"role": "system", "content": "\n\n".join(system)}] if system else []
        sent += [m.model_dump() for m in messages if m.role != "system"]
        body: dict[str, Any] = {"model": self.model, "max_tokens": self._max_tokens}
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._effort:
            body["reasoning_effort"] = self._effort
        if response_schema is not None and self._json_mode:
            body["response_format"] = {"type": "json_object"}
        body["messages"] = sent

        started = self._clock()
        data = self._post(body)
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(f"LLM API reply had no message: {str(data)[:300]}") from e
        reason = choice.get("finish_reason")
        if reason == "length":
            raise LLMError(f"the reply was cut off at {self._max_tokens} tokens")
        if reason == "content_filter":
            raise LLMError("the provider's content filter blocked the reply")

        usage = data.get("usage") or {}
        return Response(
            content=extract_json(text) if response_schema is not None else text,
            model=data.get("model") or self.model,
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            latency_seconds=round(self._clock() - started, 3),
            cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
        )
