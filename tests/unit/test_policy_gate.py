from pathlib import Path

import httpx
import pytest

from heimdall.policy.gate import Decision, LogOnlyGate, OpaEvalGate, OpaHttpGate

OPA = Path(__file__).parents[2] / ".bin" / "opa"
needs_opa = pytest.mark.skipif(not OPA.exists(), reason="run infra/tools.sh to download opa")

FRONTEND = {"kind": "Deployment", "namespace": "boutique", "name": "frontend"}


def request(action: dict, **overrides) -> dict:
    base = {
        "action": {"namespace": "boutique", "name": "frontend", **action},
        "current": {"replicas": 2, "stateful": False, "limits": {"memory": "128Mi", "cpu": "200m"}},
        "incident": {"confidence": 0.9, "affected_resources": [FRONTEND], "affected_files": [],
                     "write_actions_so_far": 0},
        "recent_actions": [],
        "revision_history": [{"revision": 6, "available_minutes": 1440}],
        "mode": "live",
        "frozen": False,
    }  # fmt: skip
    return {**base, **overrides}


@needs_opa
def test_real_policy_allows_denies_and_asks() -> None:
    gate = OpaEvalGate(OPA)

    assert gate.decide(request({"type": "restart_deployment"})) == Decision(decision="allow")
    denied = gate.decide(request({"type": "scale_deployment", "replicas": 0}))
    assert denied.decision == "deny"
    assert any("between 1 and 5" in reason for reason in denied.reasons)
    stateful = request({"type": "restart_deployment"}, current={"replicas": 1, "stateful": True})
    assert gate.decide(stateful).decision == "require_approval"


@needs_opa
def test_incomplete_input_is_denied_not_allowed() -> None:
    """A proposal with no incident context must never slip through."""
    bare = {"action": {"type": "restart_deployment", "namespace": "boutique", "name": "x"}}
    decision = OpaEvalGate(OPA).decide(bare)
    assert decision.decision == "deny"


def test_missing_binary_fails_closed() -> None:
    decision = OpaEvalGate("/nonexistent/opa").decide(request({"type": "restart_deployment"}))

    assert decision.decision == "deny"
    assert "failed closed" in decision.reasons[0]


def http_gate(handler) -> OpaHttpGate:
    return OpaHttpGate(httpx.Client(transport=httpx.MockTransport(handler), base_url="http://opa"))


def test_http_gate_returns_the_policy_result() -> None:
    result = {"decision": "require_approval", "reasons": ["target is stateful"]}
    gate = http_gate(lambda r: httpx.Response(200, json={"result": result}))

    decision = gate.decide(request({"type": "scale_deployment", "replicas": 3}))

    assert (decision.decision, decision.reasons) == ("require_approval", ["target is stateful"])


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={}),  # policy undefined
        httpx.Response(200, json={"result": {"decision": "yes"}}),
        httpx.Response(200, text="not json"),
    ],
)
def test_http_gate_fails_closed_on_anything_unexpected(response: httpx.Response) -> None:
    assert http_gate(lambda r: response).decide(request({"type": "none"})).decision == "deny"


def test_http_gate_fails_closed_when_opa_is_unreachable() -> None:
    def refuse(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no sidecar")

    assert http_gate(refuse).decide(request({"type": "none"})).decision == "deny"


# ------------------------------------------------------------- log-only gate


class _Fixed:
    def __init__(self, decision: str, reasons: list[str]) -> None:
        self.answer = Decision(decision=decision, reasons=reasons)

    def decide(self, policy_input):
        return self.answer


def test_log_only_gate_lets_a_denied_dry_run_through_and_says_so() -> None:
    gate = LogOnlyGate(_Fixed("deny", ["replicas must be at least 1"]))
    decision = gate.decide({"mode": "dry_run", "frozen": False})
    assert decision.allowed
    assert "it said deny: replicas must be at least 1" in decision.reasons[0]


@pytest.mark.parametrize(
    "policy_input",
    [{"mode": "live", "frozen": False}, {"mode": "dry_run", "frozen": True}, {}],
)
def test_log_only_gate_never_overrides_outside_an_unfrozen_dry_run(policy_input: dict) -> None:
    gate = LogOnlyGate(_Fixed("deny", ["no"]))
    assert gate.decide(policy_input).decision == "deny"
    assert (
        LogOnlyGate(_Fixed("require_approval", ["stateful"])).decide(policy_input).decision
        == "require_approval"
    )


def test_log_only_gate_keeps_a_fail_closed_denial() -> None:
    gate = LogOnlyGate(_Fixed("deny", ["policy gate failed closed: ConnectError"]))
    assert gate.decide({"mode": "dry_run", "frozen": False}).decision == "deny"


def test_log_only_gate_passes_an_allow_through_unchanged() -> None:
    assert LogOnlyGate(_Fixed("allow", [])).decide({"mode": "dry_run"}).reasons == []
