"""The provider-agnostic LLM interface."""

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class Response:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_seconds: float
    cached_tokens: int = 0
    cost_usd: float = 0.0


class LLMError(Exception):
    """The provider could not be reached or returned something unusable."""


class LLM(Protocol):
    model: str

    def complete(
        self, messages: list[Message], response_schema: dict[str, Any] | None = None
    ) -> Response:
        """Return the model's reply. With `response_schema`, the reply is JSON
        that follows the schema."""
        ...


def extract_json(text: str) -> str:
    """The JSON object in a reply, without a code fence or words around it."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text.strip()


def schema_instruction(schema: dict[str, Any]) -> str:
    """For providers whose own structured output cannot express Heimdall's
    schemas: the schema as an instruction. The callers' validation and retry
    do the enforcing."""
    # Sorted keys: the same schema must always produce the same bytes, or a
    # prompt cache would miss on every call.
    return (
        "Reply with exactly one JSON object and nothing else: no prose, no code fence. "
        "It must be valid against this JSON Schema:\n" + json.dumps(schema, sort_keys=True)
    )
