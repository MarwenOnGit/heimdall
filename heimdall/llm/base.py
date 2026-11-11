"""The provider-agnostic LLM interface."""

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
