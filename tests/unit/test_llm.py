import json
from pathlib import Path

import httpx
import pytest

from heimdall.llm.base import LLMError, Message, Response
from heimdall.llm.ollama import OllamaLLM
from heimdall.llm.replay import FixtureStale, RecordingLLM, ReplayLLM

MESSAGES = [Message(role="system", content="You are an SRE."), Message(role="user", content="hi")]
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


def ollama(handler) -> OllamaLLM:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ollama")
    return OllamaLLM("qwen3:8b", client)


def test_ollama_request_and_response_mapping() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"answer":"ok"}'},
                                         "prompt_eval_count": 120, "eval_count": 9})  # fmt: skip

    response = ollama(handler).complete(MESSAGES, SCHEMA)

    assert seen["model"] == "qwen3:8b"
    assert seen["stream"] is False
    assert seen["format"] == SCHEMA
    assert seen["options"]["temperature"] == 0
    assert seen["messages"][0] == {"role": "system", "content": "You are an SRE."}
    assert (response.content, response.prompt_tokens, response.completion_tokens) == (
        '{"answer":"ok"}', 120, 9)  # fmt: skip


def test_ollama_failure_is_an_llm_error() -> None:
    with pytest.raises(LLMError):
        ollama(lambda request: httpx.Response(500, text="model not loaded")).complete(MESSAGES)


class CountingLLM:
    model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, response_schema=None) -> Response:
        self.calls += 1
        return Response(f"answer {self.calls}", self.model, 10, 2, 0.5)


def test_replay_returns_what_was_recorded_without_calling_the_provider(tmp_path: Path) -> None:
    inner = CountingLLM()
    recorded = RecordingLLM(inner, tmp_path, "v1").complete(MESSAGES, SCHEMA)

    replayed = ReplayLLM(tmp_path, "test-model", "v1").complete(MESSAGES, SCHEMA)

    assert replayed == recorded
    assert inner.calls == 1


@pytest.mark.parametrize(
    "change",
    ["message", "prompt_version", "model", "schema"],
)
def test_any_change_to_the_request_makes_the_fixture_stale(tmp_path: Path, change: str) -> None:
    RecordingLLM(CountingLLM(), tmp_path, "v1").complete(MESSAGES, SCHEMA)

    messages = (
        MESSAGES[:-1] + [Message(role="user", content="hi!")] if change == "message" else MESSAGES
    )
    version = "v2" if change == "prompt_version" else "v1"
    model = "other-model" if change == "model" else "test-model"
    schema = {"type": "object"} if change == "schema" else SCHEMA

    with pytest.raises(FixtureStale, match="record them again"):
        ReplayLLM(tmp_path, model, version).complete(messages, schema)
