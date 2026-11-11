from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from heimdall.llm.anthropic import AnthropicLLM, estimate_cost, extract_json
from heimdall.llm.base import LLMError, Message

SCHEMA = {"type": "object", "properties": {"b": {"type": "number"}, "a": {"type": "string"}}}
MESSAGES = [
    Message(role="system", content="You are Heimdall."),
    Message(role="user", content="Incident inc_1"),
    Message(role="assistant", content='{"tool": "pods"}'),
    Message(role="user", content="tool output"),
]


class FakeClient:
    """Stands in for `anthropic.Anthropic`: records the request, returns a reply."""

    def __init__(self, text='{"a": "x"}', stop_reason="end_turn", error=None, **usage) -> None:
        self.requests: list[dict] = []
        self._error = error
        counts = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, **usage}  # fmt: skip
        self._reply = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=text)],
            stop_reason=stop_reason,
            model="claude-opus-5-5",
            usage=SimpleNamespace(**counts),
        )  # fmt: skip
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        if self._error:
            raise self._error
        return self._reply


def test_request_separates_system_from_the_conversation_and_caches_both() -> None:
    client = FakeClient()
    AnthropicLLM(client=client).complete(MESSAGES, SCHEMA)  # type: ignore[arg-type]

    (request,) = client.requests
    assert request["model"] == "claude-opus-5-5"
    assert [m["role"] for m in request["messages"]] == ["user", "assistant", "user"]
    prompt, instruction = request["system"]
    assert prompt == {"type": "text", "text": "You are Heimdall."}
    assert instruction["cache_control"] == {"type": "ephemeral"}
    assert request["cache_control"] == {"type": "ephemeral"}
    # Current models reject sampling parameters and fixed thinking budgets.
    assert not {"temperature", "top_p", "thinking"} & set(request)


def test_schema_instruction_is_byte_stable_so_the_cache_can_hit() -> None:
    client = FakeClient()
    llm = AnthropicLLM(client=client)  # type: ignore[arg-type]
    llm.complete(MESSAGES, SCHEMA)
    llm.complete(MESSAGES, {"properties": dict(reversed(SCHEMA["properties"].items())),
                            "type": "object"})  # fmt: skip

    first, second = (r["system"][-1]["text"] for r in client.requests)
    assert first == second and '"a": {"type": "string"}' in first


def test_reply_is_reduced_to_its_json_object_and_usage_is_recorded() -> None:
    client = FakeClient('Here you go:\n```json\n{"a": "x"}\n```', input_tokens=50,
                        cache_read_input_tokens=900, cache_creation_input_tokens=200)  # fmt: skip
    response = AnthropicLLM(client=client).complete(MESSAGES, SCHEMA)  # type: ignore[arg-type]

    assert response.content == '{"a": "x"}'
    assert (response.prompt_tokens, response.cached_tokens) == (1150, 900)
    assert response.completion_tokens == 20 and response.model == "claude-opus-5-5"
    assert response.cost_usd == estimate_cost("claude-opus-5-5", 50, 900, 200, 20) > 0


def test_plain_text_reply_is_returned_as_is_without_a_schema() -> None:
    client = FakeClient("all good {not json")
    assert AnthropicLLM(client=client).complete(MESSAGES).content == "all good {not json"  # type: ignore[arg-type]
    assert len(client.requests[0]["system"]) == 1


def test_fallbacks_and_effort_are_optional() -> None:
    on, off = FakeClient(), FakeClient()
    AnthropicLLM(client=on, effort="low").complete(MESSAGES)  # type: ignore[arg-type]
    AnthropicLLM(client=off, fallbacks=False).complete(MESSAGES)  # type: ignore[arg-type]

    assert on.requests[0]["fallbacks"] == "default"
    assert on.requests[0]["betas"] == ["server-side-fallback-2026-07-01"]
    assert on.requests[0]["output_config"] == {"effort": "low"}
    assert not {"fallbacks", "betas", "output_config"} & set(off.requests[0])


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_and_cut_off_replies_are_errors(stop_reason: str) -> None:
    client = FakeClient(stop_reason=stop_reason)
    with pytest.raises(LLMError):
        AnthropicLLM(client=client).complete(MESSAGES, SCHEMA)  # type: ignore[arg-type]


def test_api_failures_become_llm_errors() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    unreachable = anthropic.APIConnectionError(request=request)
    limited = anthropic.RateLimitError(
        "slow down", response=httpx2.Response(429, request=request), body=None
    )
    for error, expected in ((unreachable, "could not be reached"), (limited, "returned 429")):
        with pytest.raises(LLMError, match=expected):
            AnthropicLLM(client=FakeClient(error=error)).complete(MESSAGES)  # type: ignore[arg-type]


def test_cost_estimate() -> None:
    # 1M fresh input + 1M output on Opus 5.5 is $4 + $20.
    assert estimate_cost("claude-opus-5-5", 1_000_000, 0, 0, 1_000_000) == 24.0
    assert estimate_cost("claude-opus-5-5", 0, 1_000_000, 0, 0) == 0.2
    assert estimate_cost("claude-opus-5-5", 0, 0, 1_000_000, 0) == 5.0
    assert estimate_cost("some-future-model", 10, 10, 10, 10) == 0.0


def test_extract_json() -> None:
    assert extract_json('{"a": {"b": 1}}') == '{"a": {"b": 1}}'
    assert extract_json('text {"a": 1} more') == '{"a": 1}'
    assert extract_json("no object here") == "no object here"


def test_setup_builds_the_provider_with_the_default_model(monkeypatch) -> None:
    from heimdall.agent.setup import build_llm
    from heimdall.config import Config

    monkeypatch.setenv("HEIMDALL_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("HEIMDALL_LLM_MODEL", raising=False)
    llm = build_llm(Config.from_env())
    assert isinstance(llm, AnthropicLLM) and llm.model == "claude-opus-5-5"
