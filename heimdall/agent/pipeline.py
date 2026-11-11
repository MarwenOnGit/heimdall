"""From a diagnosis to a decision on every proposed action.

    diagnosis -> planner -> policy input (from the cluster) -> policy gate
              -> executor (dry run) -> incident state

Proposals are handled in order. The first one that is denied, rejected or
fails ends the plan: later steps were chosen assuming the earlier ones worked.
"""

from dataclasses import dataclass, field

from heimdall.agent.planner import Planner
from heimdall.core.actions import ActionStatus
from heimdall.core.audit import AuditLog, EventType
from heimdall.core.incidents import IncidentManager
from heimdall.core.models import ActionProposal, ActionType, Diagnosis, IncidentState
from heimdall.executor.executor import Executor, Outcome
from heimdall.policy.gate import Decision, PolicyGate
from heimdall.policy.input_builder import InputBuilder, TargetMissing

# Outcomes after which the next proposal may still be handled.
CONTINUE = {ActionStatus.DRY_RUN, ActionStatus.EXECUTED, ActionStatus.RECORDED}


@dataclass
class Remediation:
    steps: list[tuple[ActionProposal, Decision, Outcome]] = field(default_factory=list)
    final_state: IncidentState = IncidentState.ESCALATED
    summary: str = ""


class Pipeline:
    def __init__(
        self,
        planner: Planner,
        builder: InputBuilder,
        gate: PolicyGate,
        executor: Executor,
        manager: IncidentManager,
        audit: AuditLog,
    ) -> None:
        self._planner = planner
        self._builder = builder
        self._gate = gate
        self._executor = executor
        self._manager = manager
        self._audit = audit

    def remediate(self, diagnosis: Diagnosis) -> Remediation:
        incident_id = diagnosis.incident_id
        self._manager.transition(incident_id, IncidentState.PLANNING, reason="planning")
        plan = self._planner.plan(diagnosis)

        result = Remediation()
        for proposal in plan.proposals:
            decision, version = self._decide(proposal, diagnosis)
            outcome = self._executor.execute(incident_id, proposal, decision, version)
            result.steps.append((proposal, decision, outcome))
            if outcome.status not in CONTINUE:
                break

        result.final_state, result.summary = self._conclude(result)
        self._manager.transition(incident_id, result.final_state, reason=result.summary)
        return result

    def _decide(
        self, proposal: ActionProposal, diagnosis: Diagnosis
    ) -> tuple[Decision, str | None]:
        try:
            built = self._builder.build(proposal, diagnosis)
        except TargetMissing as e:
            decision = Decision(decision="deny", reasons=[f"{e} does not exist"])
            version = None
        else:
            decision = self._gate.decide(built.data)
            version = built.resource_version
        self._audit.append(
            EventType.POLICY_DECISION,
            {
                "proposal": proposal.model_dump(mode="json", exclude_none=True),
                "decision": decision.decision,
                "reasons": decision.reasons,
            },
            actor="policy_gate",
            incident_id=diagnosis.incident_id,
        )
        return decision, version

    def _conclude(self, result: Remediation) -> tuple[IncidentState, str]:
        """The incident's next state, and one line saying why."""
        types = [proposal.type for proposal, _, _ in result.steps]
        if types == [ActionType.NONE] and result.steps[0][2].status is ActionStatus.RECORDED:
            return IncidentState.RESOLVED, "nothing is wrong; no action taken"

        parts = []
        for proposal, _, outcome in result.steps:
            target = f" {proposal.name}" if proposal.name else ""
            parts.append(f"{proposal.type}{target}: {outcome.status} ({outcome.detail})")
        # Nothing here fixes the incident on its own yet: dry runs change
        # nothing, and approvals and pull requests need a person.
        return IncidentState.ESCALATED, "; ".join(parts) or "no proposals"
