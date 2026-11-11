"""Invariants of the action policy, checked against the real OPA engine with
randomly generated proposals. Whatever the planner proposes, these must hold
for anything the policy lets through."""

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from heimdall.policy.gate import OpaEvalGate

OPA = Path(__file__).parents[2] / ".bin" / "opa"
pytestmark = pytest.mark.skipif(not OPA.exists(), reason="run infra/tools.sh to download opa")

GATE = OpaEvalGate(OPA)
WRITES = ["restart_deployment", "rollback_deployment", "scale_deployment", "patch_resources"]
NAMES = ["frontend", "cartservice", "redis-cart", "emailservice"]
NAMESPACES = ["boutique", "kube-system", "monitoring", "heimdall"]
MEMORY = ["16Mi", "64Mi", "128Mi", "256Mi", "512Mi", "1Gi", "2Gi", "64Gi"]
CPU = ["50m", "100m", "200m", "400m", "1", "1500m", "8"]


def resource(name: str, namespace: str = "boutique") -> dict:
    return {"kind": "Deployment", "namespace": namespace, "name": name}


@st.composite
def proposals(draw: st.DrawFn) -> dict:
    kind = draw(st.sampled_from([*WRITES, "open_pr", "escalate", "none", "delete_namespace"]))
    action: dict = {
        "type": kind,
        "namespace": draw(st.sampled_from(NAMESPACES)),
        "name": draw(st.sampled_from(NAMES)),
        "replicas": draw(st.integers(min_value=-3, max_value=50)),
        "to_revision": draw(st.integers(min_value=1, max_value=9)),
        "limits": draw(
            st.fixed_dictionaries(
                {}, optional={"memory": st.sampled_from(MEMORY), "cpu": st.sampled_from(CPU)}
            )
        ),
        "files": draw(st.lists(st.sampled_from(["a.yaml", "b.yaml", "../etc/passwd"]), max_size=3)),
        "diff_lines": draw(st.integers(min_value=0, max_value=500)),
    }
    return {
        "action": action,
        "current": {
            "replicas": draw(st.integers(min_value=0, max_value=8)),
            "stateful": draw(st.booleans()),
            "limits": {"memory": draw(st.sampled_from(MEMORY[:5])),
                       "cpu": draw(st.sampled_from(CPU[:4]))},
        },
        "incident": {
            "confidence": draw(st.floats(min_value=0, max_value=1)),
            "affected_resources": [
                resource(n) for n in draw(st.lists(st.sampled_from(NAMES), max_size=2, unique=True))
            ],
            "affected_files": draw(st.lists(st.sampled_from(["a.yaml", "b.yaml"]), unique=True)),
            "write_actions_so_far": draw(st.integers(min_value=0, max_value=6)),
        },
        "recent_actions": draw(
            st.lists(
                st.fixed_dictionaries(
                    {"type": st.sampled_from(WRITES), "name": st.sampled_from(NAMES),
                     "minutes_ago": st.integers(min_value=0, max_value=30)}
                ),
                max_size=3,
            )
        ),
        "revision_history": draw(
            st.lists(
                st.fixed_dictionaries(
                    {"revision": st.integers(min_value=1, max_value=9),
                     "available_minutes": st.integers(min_value=0, max_value=100)}
                ),
                max_size=4,
            )
        ),
        "mode": draw(st.sampled_from(["live", "dry_run", "observe"])),
        "frozen": draw(st.booleans()),
    }  # fmt: skip


MEMORY_BYTES = {"16Mi": 16, "64Mi": 64, "128Mi": 128, "256Mi": 256, "512Mi": 512, "1Gi": 1024,
                "2Gi": 2048, "64Gi": 65536}  # fmt: skip
CPU_MILLI = {"50m": 50, "100m": 100, "200m": 200, "400m": 400, "1": 1000, "1500m": 1500, "8": 8000}


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(policy_input=proposals())
def test_nothing_unsafe_is_ever_let_through(policy_input: dict) -> None:
    decision = GATE.decide(policy_input)
    if decision.decision == "deny":
        return

    action, incident = policy_input["action"], policy_input["incident"]
    kind = action["type"]

    assert not policy_input["frozen"]
    assert kind in {*WRITES, "open_pr", "escalate", "none"}
    if kind in {"escalate", "none"}:
        return

    assert incident["confidence"] >= 0.8
    assert policy_input["mode"] != "observe"

    if kind in WRITES:
        assert action["namespace"] == "boutique"
        assert resource(action["name"]) in incident["affected_resources"]
        assert incident["write_actions_so_far"] < 3
        assert not any(
            r["type"] == kind and r["name"] == action["name"] and r["minutes_ago"] < 10
            for r in policy_input["recent_actions"]
        )
        if policy_input["current"]["stateful"]:
            assert decision.decision == "require_approval"

    if kind == "scale_deployment":
        assert 1 <= action["replicas"] <= 5
        assert -1 <= action["replicas"] - policy_input["current"]["replicas"] <= 2

    if kind == "patch_resources":
        limits, current = action["limits"], policy_input["current"]["limits"]
        assert limits
        if "memory" in limits:
            assert MEMORY_BYTES[current["memory"]] <= MEMORY_BYTES[limits["memory"]] <= 1024
            if MEMORY_BYTES[limits["memory"]] > 2 * MEMORY_BYTES[current["memory"]]:
                assert decision.decision == "require_approval"
        if "cpu" in limits:
            assert CPU_MILLI[current["cpu"]] <= CPU_MILLI[limits["cpu"]] <= 1000
            if CPU_MILLI[limits["cpu"]] > 2 * CPU_MILLI[current["cpu"]]:
                assert decision.decision == "require_approval"

    if kind == "rollback_deployment":
        assert any(
            r["revision"] == action["to_revision"] and r["available_minutes"] >= 10
            for r in policy_input["revision_history"]
        )

    if kind == "open_pr":
        assert set(action["files"]) <= set(incident["affected_files"])
        assert action["diff_lines"] <= 50
