"""The policy gate: asks OPA whether a proposed action may run.

The gate fails closed. If OPA cannot be reached, answers with something
unexpected, or the policy itself errors, the decision is `deny`.
"""

import json
import subprocess
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ValidationError

POLICY_DIR = Path(__file__).parent / "policies"
QUERY = "data.heimdall.actions.result"


class Decision(BaseModel):
    decision: Literal["allow", "require_approval", "deny"]
    reasons: list[str] = []

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


def _closed(reason: str) -> Decision:
    return Decision(decision="deny", reasons=[f"policy gate failed closed: {reason}"])


class PolicyGate(Protocol):
    def decide(self, policy_input: dict[str, Any]) -> Decision: ...


class OpaHttpGate:
    """Talks to an OPA server (the sidecar in the cluster)."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def decide(self, policy_input: dict[str, Any]) -> Decision:
        try:
            response = self._client.post(
                "/v1/data/heimdall/actions/result", json={"input": policy_input}
            )
            response.raise_for_status()
            return Decision.model_validate(response.json()["result"])
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as e:
            return _closed(type(e).__name__)


class OpaEvalGate:
    """Runs the `opa` binary directly. Used in tests and on a developer machine."""

    def __init__(self, opa: Path | str = "opa", policy_dir: Path = POLICY_DIR) -> None:
        self._opa = str(opa)
        self._policy_dir = policy_dir

    def decide(self, policy_input: dict[str, Any]) -> Decision:
        try:
            result = subprocess.run(  # noqa: S603 - fixed arguments, input on stdin
                [self._opa, "eval", "--format", "json", "--stdin-input",
                 "--data", str(self._policy_dir), QUERY],
                input=json.dumps(policy_input),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )  # fmt: skip
            value = json.loads(result.stdout)["result"][0]["expressions"][0]["value"]
            return Decision.model_validate(value)
        except (OSError, subprocess.SubprocessError, KeyError, IndexError, ValueError) as e:
            return _closed(type(e).__name__)


class LogOnlyGate:
    """Asks the real gate, records its verdict, and lets the action through.

    For one benchmark question only: how many of the model's proposals would
    have been unsafe without the gate? That measures how much of the safety
    comes from the gate and how much from the model.

    It can only ever let a dry run through. In any other mode, when Heimdall
    is frozen, or when the real gate could not decide, the real verdict
    stands. The API server's admission policy still judges every dry run.
    """

    def __init__(self, inner: PolicyGate) -> None:
        self._inner = inner

    def decide(self, policy_input: dict[str, Any]) -> Decision:
        real = self._inner.decide(policy_input)
        failed_closed = any(r.startswith("policy gate failed closed") for r in real.reasons)
        if (
            real.allowed
            or policy_input.get("mode") != "dry_run"
            or policy_input.get("frozen")
            or failed_closed
        ):
            return real
        verdict = f"{real.decision}: {'; '.join(real.reasons)}"
        return Decision(decision="allow", reasons=[f"gate in log-only mode; it said {verdict}"])
