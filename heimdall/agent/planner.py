"""The remediation planner: from a validated diagnosis to action proposals.

The model chooses, but only within limits that are enforced here in code,
not in the prompt:

- each root cause category allows a fixed set of action types,
- an action may only target a resource named in the diagnosis,
- below the confidence threshold the only outcome is escalation.

A proposal that breaks a limit is dropped and the incident is escalated
instead. Whatever survives still has to pass the policy gate and, for cluster
writes, the admission policy.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from heimdall.core.audit import AuditLog, EventType
from heimdall.core.models import (
    ActionProposal,
    ActionType,
    Diagnosis,
    ResourceRef,
    RootCause,
    RootCauseCategory,
)
from heimdall.llm.base import LLM, LLMError, Message

PROMPT_PATH = Path(__file__).parents[1] / "llm" / "prompts" / "planner.md"

A = ActionType
C = RootCauseCategory

# What can possibly help for each kind of cause. The admission policy forbids
# the actor from changing env vars, probes and volumes, so causes of that kind
# can only be fixed through git or by a human.
ALLOWED: dict[RootCauseCategory, frozenset[ActionType]] = {
    C.BAD_CONFIG: frozenset({A.OPEN_PR, A.ESCALATE}),
    C.MISSING_DEPENDENCY: frozenset({A.ESCALATE}),
    C.RESOURCE_LIMITS_TOO_LOW: frozenset({A.PATCH_RESOURCES, A.OPEN_PR, A.ESCALATE}),
    C.BAD_RELEASE: frozenset({A.ROLLBACK_DEPLOYMENT, A.OPEN_PR, A.ESCALATE}),
    C.BAD_IMAGE_REFERENCE: frozenset({A.ROLLBACK_DEPLOYMENT, A.OPEN_PR, A.ESCALATE}),
    C.BAD_PROBE_CONFIG: frozenset({A.OPEN_PR, A.ESCALATE}),
    C.DEPENDENCY_DOWN: frozenset({A.SCALE_DEPLOYMENT, A.RESTART_DEPLOYMENT, A.ESCALATE}),
    C.RESOURCE_SATURATION: frozenset({A.SCALE_DEPLOYMENT, A.ESCALATE}),
    C.NETWORK_DEGRADATION: frozenset({A.ESCALATE}),
    C.DISK_PRESSURE: frozenset({A.ESCALATE}),
    C.NODE_PROBLEM: frozenset({A.ESCALATE}),
    C.NO_ACTION_NEEDED: frozenset({A.NONE, A.ESCALATE}),
    C.UNKNOWN: frozenset({A.ESCALATE}),
}

PASSIVE = frozenset({A.ESCALATE, A.NONE})


class _Reply(BaseModel):
    proposals: list[ActionProposal]


@dataclass
class Plan:
    proposals: list[ActionProposal]
    # Proposals the model made that broke a limit, with the reason.
    dropped: list[tuple[ActionProposal, str]] = field(default_factory=list)
    reason: str = "planned"


def escalate(summary: str, why: str) -> ActionProposal:
    return ActionProposal(
        type=A.ESCALATE,
        summary=summary,
        justification=why,
        expected_effect="a human takes over the incident",
    )


def _same(a: ResourceRef | None, b: ResourceRef | None) -> bool:
    return a is not None and b is not None and (a.kind, a.namespace, a.name) == (
        b.kind, b.namespace, b.name)  # fmt: skip


def check(proposal: ActionProposal, diagnosis: Diagnosis) -> str | None:
    """Why a proposal is not acceptable for this diagnosis, or None if it is."""
    if proposal.type in PASSIVE:
        if proposal.type is A.NONE and any(
            cause.category is not C.NO_ACTION_NEEDED for cause in diagnosis.root_causes
        ):
            return "`none` is only acceptable when nothing is wrong"
        return None
    if proposal.target is None:
        return f"{proposal.type} needs a target Deployment"
    matching = [c for c in diagnosis.root_causes if _same(c.affected_resource, proposal.target)]
    if not matching:
        return f"{proposal.target.name} is not a resource named in the diagnosis"
    if not any(proposal.type in ALLOWED[cause.category] for cause in matching):
        categories = ", ".join(cause.category for cause in matching)
        return f"{proposal.type} is not an allowed action for {categories}"
    required = {
        A.SCALE_DEPLOYMENT: proposal.replicas,
        A.ROLLBACK_DEPLOYMENT: proposal.to_revision,
        A.PATCH_RESOURCES: proposal.limits,
    }
    if proposal.type in required and required[proposal.type] is None:
        return f"{proposal.type} is missing its parameters"
    return None


def _allowed_line(cause: RootCause) -> str:
    resource = cause.affected_resource
    where = f"{resource.kind} {resource.name}" if resource else "(no resource)"
    return f"- {cause.category} on {where}: {', '.join(sorted(ALLOWED[cause.category]))}"


class Planner:
    def __init__(self, llm: LLM, audit: AuditLog, *, confidence_threshold: float = 0.8) -> None:
        self._llm = llm
        self._audit = audit
        self._threshold = confidence_threshold
        self._template = PROMPT_PATH.read_text()

    def plan(self, diagnosis: Diagnosis) -> Plan:
        plan = self._plan(diagnosis)
        self._audit.append(
            EventType.PROPOSAL,
            {
                "proposals": [p.model_dump(mode="json", exclude_none=True) for p in plan.proposals],
                "dropped": [
                    {"proposal": p.model_dump(mode="json", exclude_none=True), "reason": why}
                    for p, why in plan.dropped
                ],
                "reason": plan.reason,
            },
            actor="planner",
            incident_id=diagnosis.incident_id,
        )
        return plan

    def _plan(self, diagnosis: Diagnosis) -> Plan:
        summary = "; ".join(cause.summary for cause in diagnosis.root_causes)
        categories = {cause.category for cause in diagnosis.root_causes}

        # Decided without the model: these have exactly one acceptable outcome.
        if diagnosis.confidence < self._threshold:
            why = f"confidence {diagnosis.confidence:.2f} is below {self._threshold}"
            return Plan([escalate(summary, why)], reason=why)
        if categories == {C.NO_ACTION_NEEDED}:
            nothing = ActionProposal(
                type=A.NONE, summary=summary, justification="the diagnosis found nothing wrong",
                expected_effect="no change",
            )  # fmt: skip
            return Plan([nothing], reason="nothing is wrong")
        if all(ALLOWED[category] <= PASSIVE for category in categories):
            why = "no action in the catalog can fix this kind of cause"
            return Plan([escalate(summary, why)], reason=why)

        allowed = "\n".join(_allowed_line(cause) for cause in diagnosis.root_causes)
        prompt = self._template.format(
            diagnosis=diagnosis.model_dump_json(indent=2), allowed=allowed
        )
        messages = [Message(role="user", content=prompt)]

        reply: _Reply | None = None
        for _attempt in range(2):
            try:
                response = self._llm.complete(messages, _Reply.model_json_schema())
            except LLMError as e:
                return Plan([escalate(summary, f"planner unavailable: {e}")], reason="LLM failure")
            self._audit.append(
                EventType.LLM_CALL,
                {"model": response.model, "purpose": "plan", "reply": response.content,
                 "prompt_tokens": response.prompt_tokens,
                 "completion_tokens": response.completion_tokens,
                 "latency_seconds": response.latency_seconds},
                actor="planner",
                incident_id=diagnosis.incident_id,
            )  # fmt: skip
            try:
                reply = _Reply.model_validate_json(response.content)
                break
            except ValidationError as e:
                problems = "; ".join(err["msg"] for err in e.errors()[:5])
                messages += [
                    Message(role="assistant", content=response.content),
                    Message(role="user", content=f"Not valid: {problems}. Reply again."),
                ]
        if reply is None:
            why = "the planner did not produce a usable reply"
            return Plan([escalate(summary, why)], reason=why)

        kept: list[ActionProposal] = []
        dropped: list[tuple[ActionProposal, str]] = []
        for proposal in reply.proposals:
            problem = check(proposal, diagnosis)
            if problem is None:
                kept.append(proposal)
            else:
                dropped.append((proposal, problem))

        if dropped or not kept:
            # A plan that had to be corrected is not trusted in part: a human
            # gets the whole picture instead.
            why = "; ".join(reason for _, reason in dropped) or "no proposals"
            return Plan([escalate(summary, f"the plan broke a limit: {why}")], dropped, reason=why)
        return Plan(kept, reason="planned")


def describe(plan: Plan) -> str:
    """One line per proposal, for logs and notifications."""
    lines = []
    for proposal in plan.proposals:
        details = proposal.model_dump(
            mode="json", exclude_none=True, exclude={"type", "justification", "expected_effect"}
        )
        lines.append(f"{proposal.type} {json.dumps(details, sort_keys=True)}")
    return "\n".join(lines)


class ForcedPlanner:
    """Proposes a fixed plan, whatever the diagnosis. For the benchmark only.

    Scenario s15 needs a wrong fix to be attempted so that verification and
    undo can be observed. The model cannot be relied on to be wrong on cue,
    and the real planner would drop the wrong fix, so the harness forces it.

    A forced plan skips the planner's own limits and nothing else: the policy
    gate, the API server, verification and undo all still apply. It is marked
    as forced in the audit log, and so in the postmortem.
    """

    def __init__(self, proposals: list[ActionProposal], audit: AuditLog) -> None:
        self._proposals = proposals
        self._audit = audit

    @classmethod
    def from_json(cls, raw: str, audit: AuditLog) -> "ForcedPlanner":
        return cls(_Reply.model_validate_json(raw).proposals, audit)

    def plan(self, diagnosis: Diagnosis) -> Plan:
        plan = Plan(list(self._proposals), reason="forced by the benchmark harness")
        self._audit.append(
            EventType.PROPOSAL,
            {
                "proposals": [p.model_dump(mode="json", exclude_none=True) for p in plan.proposals],
                "dropped": [],
                "reason": plan.reason,
                "forced": True,
            },
            actor="benchmark",
            incident_id=diagnosis.incident_id,
        )
        return plan
