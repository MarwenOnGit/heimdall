"""Claude through the Anthropic API.

What leaves the machine is what the investigator and planner already see:
tool output after redaction and sanitization. The reply is treated exactly
like a local model's: parsed, validated, and rejected if it does not hold up.

Heimdall's reply schemas use things strict structured output does not accept
(number ranges, free-form argument objects), so the schema is given to the
model as an instruction instead, and the callers' own validation and retry
do the enforcing, as they do for every provider.
"""

import time
from typing import Any

import anthropic

from heimdall.llm.base import LLMError, Message, Response, extract_json, schema_instruction

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: (input, output, cache read). Cache writes cost
# 1.25x input. Models that are not listed are recorded with a cost of 0.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
_CACHE_WRITE = 1.25


def estimate_cost(model: str, fresh: int, cache_read: int, cache_write: int, output: int) -> float:
    if model not in PRICES:
        return 0.0
    price_in, price_out, price_read = PRICES[model]
    total = (
        fresh * price_in
        + cache_write * price_in * _CACHE_WRITE
        + cache_read * price_read
        + output * price_out
    )
    return round(total / 1_000_000, 6)


class AnthropicLLM:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        client: anthropic.Anthropic | None = None,
        *,
        max_tokens: int = 16000,
        effort: str | None = None,
        fallbacks: bool = True,
    ) -> None:
        self.model = model
        # Credentials come from the environment (ANTHROPIC_API_KEY or a login
        # profile); they are never read or stored by Heimdall.
        self._client = client or anthropic.Anthropic()
        self._max_tokens = max_tokens
        self._effort = effort
        self._fallbacks = fallbacks

    def complete(
        self, messages: list[Message], response_schema: dict[str, Any] | None = None
    ) -> Response:
        system: list[dict[str, Any]] = [
            {"type": "text", "text": m.content} for m in messages if m.role == "system"
        ]
        if response_schema is not None:
            system.append({"type": "text", "text": schema_instruction(response_schema)})
        if system:
            # Everything up to here is the same for every call of an
            # investigation and across investigations.
            system[-1]["cache_control"] = {"type": "ephemeral"}

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages if m.role != "system"
            ],
            # Also cache the conversation so far; each turn only adds to it.
            "cache_control": {"type": "ephemeral"},
        }
        if self._effort:
            request["output_config"] = {"effort": self._effort}
        if self._fallbacks:
            # If a safety classifier declines the request, the API reruns it
            # on another model. `Response.model` records which one answered.
            request.update(betas=[FALLBACK_BETA], fallbacks="default")

        started = time.monotonic()
        try:
            reply = self._client.beta.messages.create(**request)
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Anthropic API could not be reached: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API returned {e.status_code}: {e.message}") from e
        except anthropic.AnthropicError as e:
            raise LLMError(f"Anthropic request failed: {e}") from e

        if reply.stop_reason == "refusal":
            raise LLMError("the model declined to answer (refusal)")
        if reply.stop_reason == "max_tokens":
            raise LLMError(f"the reply was cut off at {self._max_tokens} tokens")
        text = next((block.text for block in reply.content if block.type == "text"), "")

        usage = reply.usage
        fresh = usage.input_tokens or 0
        cache_read = usage.cache_read_input_tokens or 0
        cache_write = usage.cache_creation_input_tokens or 0
        output = usage.output_tokens or 0
        return Response(
            content=extract_json(text) if response_schema is not None else text,
            model=reply.model,
            prompt_tokens=fresh + cache_read + cache_write,
            completion_tokens=output,
            latency_seconds=round(time.monotonic() - started, 3),
            cached_tokens=cache_read,
            cost_usd=estimate_cost(reply.model, fresh, cache_read, cache_write, output),
        )
