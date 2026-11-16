import json

import httpx
import pytest

from heimdall.llm.base import LLMError, Message
from heimdall.llm.openai_compat import OpenAICompatLLM

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
MESSAGES = [
    Message(role="system", content="You are Heimdall."),
    Message(role="user", content="Incident inc_1"),
    Message(role="assistant", content='{"tool": "pods"}'),
    Message(role="user", content="tool output"),
]


def reply(text='{"a": "x"}', finish="stop", **usage) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "served-model",
            "choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, **usage},
        },
    )  # fmt: skip


class Api:
    """A scripted API: answers in order and records requests and waits."""

    def __init__(self, *responses: httpx.Response | Exception, **options) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.now = 1000.0
        client = httpx.Client(
            transport=httpx.MockTransport(self._handle),
            base_url="https://api.example/v1",
            headers={"Authorization": "Bearer test-key-not-real"},
        )
        self.llm = OpenAICompatLLM(
            "some-model", client, sleep=self._sleep, clock=lambda: self.now, **options
        )

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content)


def test_request_is_a_chat_completion_with_one_system_message_and_the_schema() -> None:
    api = Api(reply())
    api.llm.complete(MESSAGES, SCHEMA)

    assert api.requests[0].url == "https://api.example/v1/chat/completions"
    assert api.requests[0].headers["authorization"] == "Bearer test-key-not-real"
    body = api.body()
    assert body["model"] == "some-model" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]
    system = body["messages"][0]["content"]
    assert system.startswith("You are Heimdall.") and '"a": {"type": "string"}' in system


def test_without_a_schema_nothing_is_added() -> None:
    api = Api(reply("plain words"))
    response = api.llm.complete(MESSAGES)

    assert "response_format" not in api.body()
    assert api.body()["messages"][0]["content"] == "You are Heimdall."
    assert response.content == "plain words"


def test_json_mode_and_effort_are_options() -> None:
    api = Api(reply(), json_mode=False, effort="low", temperature=None)
    api.llm.complete(MESSAGES, SCHEMA)

    body = api.body()
    assert "response_format" not in body and "temperature" not in body
    assert body["reasoning_effort"] == "low"


def test_reply_is_reduced_to_its_json_object_and_usage_is_recorded() -> None:
    api = Api(reply('```json\n{"a": "x"}\n```', prompt_tokens_details={"cached_tokens": 60}))
    response = api.llm.complete(MESSAGES, SCHEMA)

    assert response.content == '{"a": "x"}'
    assert (response.prompt_tokens, response.completion_tokens) == (100, 20)
    assert response.cached_tokens == 60 and response.model == "served-model"
    assert response.cost_usd == 0.0


def test_rate_limit_waits_as_long_as_the_provider_asks_then_retries() -> None:
    api = Api(httpx.Response(429, headers={"retry-after": "7"}, text="slow down"), reply())
    response = api.llm.complete(MESSAGES, SCHEMA)

    assert api.sleeps == [7.0] and len(api.requests) == 2
    assert response.content == '{"a": "x"}'
    assert response.latency_seconds == 7.0  # the wait is part of what the call cost


def test_wait_without_a_header_doubles_and_is_capped() -> None:
    busy = [httpx.Response(503, text="busy") for _ in range(3)]
    api = Api(*busy, reply(), max_wait_seconds=5)
    api.llm.complete(MESSAGES)

    assert api.sleeps == [2.0, 4.0, 5.0]


def test_a_provider_that_stays_busy_is_an_llm_error() -> None:
    api = Api(*[httpx.Response(429, text="quota exceeded for today")] * 3, max_retries=2)

    with pytest.raises(LLMError, match="429: quota exceeded"):
        api.llm.complete(MESSAGES)
    assert len(api.requests) == 3


def test_requests_are_kept_apart_by_the_minimum_interval() -> None:
    api = Api(reply(), reply(), reply(), min_interval_seconds=4)
    api.llm.complete(MESSAGES)
    api.now += 1
    api.llm.complete(MESSAGES)
    api.now += 10
    api.llm.complete(MESSAGES)

    assert api.sleeps == [3.0]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, text="invalid api key"), "401"),
        (httpx.ConnectError("no route"), "could not be reached"),
        (httpx.Response(200, text="<html>gateway</html>"), "not JSON"),
        (httpx.Response(200, json={"error": "odd"}), "no message"),
        (reply(finish="length"), "cut off"),
        (reply(finish="content_filter"), "content filter"),
    ],
)
def test_failures_are_llm_errors(response, message: str) -> None:
    with pytest.raises(LLMError, match=message):
        Api(response).llm.complete(MESSAGES, SCHEMA)


def test_the_key_is_never_part_of_an_error() -> None:
    api = Api(httpx.Response(403, text="forbidden"))
    with pytest.raises(LLMError) as error:
        api.llm.complete(MESSAGES)
    assert "test-key-not-real" not in str(error.value)


def test_setup_builds_the_provider_from_the_environment(monkeypatch) -> None:
    from heimdall.agent.setup import build_llm
    from heimdall.config import Config

    monkeypatch.setenv("HEIMDALL_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HEIMDALL_LLM_BASE_URL", "https://api.example/v1/")
    monkeypatch.setenv("HEIMDALL_LLM_API_KEY", "test-key-not-real")
    monkeypatch.setenv("HEIMDALL_LLM_MODEL", "some-model")
    llm = build_llm(Config.from_env())
    assert isinstance(llm, OpenAICompatLLM) and llm.model == "some-model"

    monkeypatch.delenv("HEIMDALL_LLM_API_KEY")
    monkeypatch.delenv("HEIMDALL_LLM_MODEL")
    with pytest.raises(ValueError, match="HEIMDALL_LLM_API_KEY, HEIMDALL_LLM_MODEL"):
        build_llm(Config.from_env())
