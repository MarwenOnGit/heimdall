"""From a diagnosis to a decision, and in live mode an outcome, for every
proposed action.

    diagnosis -> planner -> policy input (from the cluster) -> policy gate
              -> [human approval] -> executor -> verifier -> [undo]

Proposals are handled in order. The first one that is denied, rejected, needs
approval or fails ends the plan: later steps were chosen assuming the earlier
ones worked. In live mode every write is verified; one that does not fix the
problem is undone and the incident is handed to a human.
"""

from dataclasses import dataclass, field

from heimdall.agent.planner import ForcedPlanner, Planner
from heimdall.core.actions import CLUSTER_WRITES, ActionStatus, ActionStore, idempotency_key
from heimdall.core.approvals import Approval, ApprovalStore
from heimdall.core.audit import AuditLog, EventType
from heimdall.core.incidents import TRANSITIONS, IncidentManager
from heimdall.core.models import ActionProposal, ActionType, Diagnosis, IncidentState
from heimdall.executor.executor import DRIFT_CREATING, Executor, Outcome
from heimdall.executor.gitops import GitOps, GitOpsError
from heimdall.executor.verifier import Verifier, recovery_checks
from heimdall.notify.webhook import Notifier, NullNotifier
from heimdall.policy.gate import Decision, PolicyGate
from heimdall.policy.input_builder import CannotBuild, InputBuilder
from heimdall.tools.kube import KubeError
from heimdall.tools.redact import redact

S = IncidentState
# Outcomes after which the next proposal may still be handled.
CONTINUE = {
    ActionStatus.DRY_RUN,
    ActionStatus.EXECUTED,
    ActionStatus.RECORDED,
    ActionStatus.PR_OPENED,
}


@dataclass
class Remediation:
    steps: list[tuple[ActionProposal, Decision, Outcome]] = field(default_factory=list)
    final_state: IncidentState = IncidentState.ESCALATED
    summary: str = ""
    awaiting: Approval | None = None
    rolled_back: bool = False


def describe(proposal: ActionProposal) -> str:
    """One line saying exactly what an action would do."""
    details = {
        ActionType.SCALE_DEPLOYMENT: f"to {proposal.replicas} replicas",
        ActionType.ROLLBACK_DEPLOYMENT: f"to revision {proposal.to_revision}",
        ActionType.PATCH_RESOURCES: f"{proposal.container or 'container'} limits {proposal.limits}",
    }.get(proposal.type, "")
    target = f" {proposal.namespace}/{proposal.name}" if proposal.name else ""
    return f"{proposal.type}{target} {details}".strip()


class Pipeline:
    def __init__(
        self,
        planner: Planner | ForcedPlanner,
        builder: InputBuilder,
        gate: PolicyGate,
        executor: Executor,
        manager: IncidentManager,
        audit: AuditLog,
        *,
        live: bool = False,
        verifier: Verifier | None = None,
        approvals: ApprovalStore | None = None,
        notifier: Notifier | None = None,
        gitops: GitOps | None = None,
        public_url: str = "http://localhost:8000",
    ) -> None:
        if live and verifier is None:
            raise ValueError("live mode needs a verifier: no unverified writes")
        self._planner = planner
        self._builder = builder
        self._gate = gate
        self._executor = executor
        self._manager = manager
        self._audit = audit
        self._live = live
        self._verifier = verifier
        self._approvals = approvals
        self._notifier = notifier or NullNotifier()
        self._gitops = gitops
        self._public_url = public_url.rstrip("/")

    # ------------------------------------------------------------ entry

    def remediate(self, diagnosis: Diagnosis) -> Remediation:
        self._move(diagnosis.incident_id, S.PLANNING, "planning")
        plan = self._planner.plan(diagnosis)
        return self._run(diagnosis, plan.proposals)

    def resume(self, approval: Approval, *, approved: bool) -> Remediation:
        """Continue an incident after a human decided on an approval."""
        incident_id = approval.incident_id
        self._audit.append(
            EventType.APPROVAL,
            {
                "approval_id": approval.id,
                "approved": approved,
                "action": describe(approval.proposal),
            },
            actor="human",
            incident_id=incident_id,
        )
        if not approved:
            return self._finish(
                incident_id, Remediation(), S.ESCALATED,
                f"a human rejected: {describe(approval.proposal)}",
            )  # fmt: skip

        diagnosis, proposals = self._recall(incident_id)
        # Pick up the plan at the approved action; earlier steps already ran.
        index = next(
            (i for i, p in enumerate(proposals)
             if idempotency_key(incident_id, p) == approval.action_hash),
            None,
        )  # fmt: skip
        if diagnosis is None or index is None:
            return self._finish(
                incident_id, Remediation(), S.ESCALATED,
                "the approved action is no longer part of the plan",
            )  # fmt: skip
        return self._run(diagnosis, proposals[index:], approval=approval)

    def expire_approvals(self) -> list[Remediation]:
        """Expired means rejected: the incident goes to a human."""
        if self._approvals is None:
            return []
        results = []
        for approval in self._approvals.expire_due():
            self._audit.append(
                EventType.APPROVAL,
                {"approval_id": approval.id, "approved": False, "expired": True},
                actor="heimdall",
                incident_id=approval.incident_id,
            )
            results.append(
                self._finish(
                    approval.incident_id,
                    Remediation(),
                    S.ESCALATED,
                    f"approval expired for: {describe(approval.proposal)}",
                )  # fmt: skip
            )
        return results

    # ------------------------------------------------------------- loop

    def _run(
        self,
        diagnosis: Diagnosis,
        proposals: list[ActionProposal],
        approval: Approval | None = None,
    ) -> Remediation:
        incident_id = diagnosis.incident_id
        result = Remediation()
        for index, proposal in enumerate(proposals):
            approved = approval if index == 0 else None
            decision, version = self._decide(proposal, diagnosis, approved)

            if decision.decision == "require_approval" and self._approvals is not None:
                result.awaiting = self._approvals.request(incident_id, proposal, version)
            if self._live and decision.allowed and proposal.type in CLUSTER_WRITES:
                self._move(incident_id, S.EXECUTING, f"executing {describe(proposal)}")

            outcome = self._executor.execute(incident_id, proposal, decision, version)
            if outcome.status is ActionStatus.EXECUTED:
                outcome = self._verify(incident_id, proposal, outcome, result)
            result.steps.append((proposal, decision, outcome))
            if outcome.status not in CONTINUE:
                break

        state, summary = self._conclude(result)
        return self._finish(incident_id, result, state, summary)

    def _decide(
        self, proposal: ActionProposal, diagnosis: Diagnosis, approval: Approval | None
    ) -> tuple[Decision, str | None]:
        version = None
        try:
            built = self._builder.build(proposal, diagnosis)
        except CannotBuild as e:
            decision = Decision(decision="deny", reasons=[str(e)])
        else:
            version = built.resource_version
            if approval is not None and version != approval.resource_version:
                # The human approved a change to something that has since
                # changed. The approval no longer means what it said.
                if self._approvals is not None:
                    self._approvals.void(approval.id)
                decision = Decision(
                    decision="deny",
                    reasons=["the target changed after approval was requested; approval is void"],
                )
            else:
                # The policy runs again after approval, on fresh state.
                decision = self._gate.decide({**built.data, "approved": approval is not None})
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

    def _verify(
        self, incident_id: str, proposal: ActionProposal, outcome: Outcome, result: Remediation
    ) -> Outcome:
        """Check that a live write worked; undo it if it did not."""
        assert self._verifier is not None  # noqa: S101 - enforced in __init__
        self._move(incident_id, S.VERIFYING, f"verifying {describe(proposal)}")
        incident = self._manager.get(incident_id)
        verification = self._verifier.verify(recovery_checks(proposal, incident))
        self._audit.append(
            EventType.VERIFICATION,
            {
                "action": describe(proposal),
                "recovered": verification.recovered,
                "seconds": verification.seconds,
                "checks": verification.checks,
            },
            actor="verifier",
            incident_id=incident_id,
        )
        if verification.recovered:
            return outcome

        undo = self._executor.undo(incident_id, proposal)
        if undo.status is ActionStatus.UNDONE and proposal.type in DRIFT_CREATING:
            self._resume_sync(incident_id)  # the cluster matches git again
        self._move(incident_id, S.ROLLED_BACK, f"{describe(proposal)} did not fix the problem")
        result.rolled_back = True
        failing = ", ".join(verification.failing)
        undone = "undone" if undo.status is ActionStatus.UNDONE else f"UNDO FAILED: {undo.detail}"
        return Outcome(undo.status, f"did not recover ({failing}); {undone}", outcome.previous)

    def _conclude(self, result: Remediation) -> tuple[IncidentState, str]:
        """The incident's next state, and one line saying why."""
        parts = []
        for proposal, _, outcome in result.steps:
            parts.append(f"{describe(proposal)}: {outcome.status} ({outcome.detail})")
        summary = "; ".join(parts) or "no proposals"

        if result.awaiting is not None:
            return S.AWAITING_APPROVAL, summary
        last = result.steps[-1][2].status if result.steps else None
        if result.rolled_back or last not in CONTINUE:
            return S.ESCALATED, summary

        types = [proposal.type for proposal, _, _ in result.steps]
        if types == [ActionType.NONE]:
            return S.RESOLVED, "nothing is wrong; no action taken"
        if any(o.status is ActionStatus.EXECUTED for _, _, o in result.steps):
            # The service is healthy again. If git still has to be brought in
            # line (a pull request is part of the plan), that is "mitigated".
            if ActionType.OPEN_PR in types:
                return S.MITIGATED, summary
            return S.RESOLVED, summary
        # Dry runs change nothing, and escalations and pull requests need a person.
        return S.ESCALATED, summary

    # ---------------------------------------------------------- restart

    def recover(self) -> list[str]:
        """Bring incidents that a restart interrupted to a safe end.

        Called once at startup. Nothing is ever applied again and the plan is
        never resumed: a human gets the incident. The one thing recovery does
        do is finish the safety step that was cut short. If a write was sent
        (or may have been) and was not verified, it is verified now, and
        undone if the service has not recovered. Returns the incidents handled.
        """
        handled = []
        interrupted = (S.DIAGNOSED, S.PLANNING, S.EXECUTING, S.VERIFYING, S.ROLLED_BACK)
        for incident in self._manager.all(active_only=True):
            if incident.state not in interrupted:
                continue
            self._recover(incident.id, incident.state)
            handled.append(incident.id)
        return handled

    def _recover(self, incident_id: str, state: IncidentState) -> None:
        _, proposals = self._recall(incident_id)
        records = [(p, self._executor.record_of(incident_id, p)) for p in proposals]
        unverified = [
            p
            for p, record in records
            if record is not None
            and p.type in CLUSTER_WRITES
            and (
                record.status is ActionStatus.PENDING
                or (record.status is ActionStatus.EXECUTED and state is S.VERIFYING)
            )
        ]
        result = Remediation()
        if not unverified or self._verifier is None:
            drift_open = any(
                record is not None
                and p.type in DRIFT_CREATING
                and record.status in (ActionStatus.EXECUTED, ActionStatus.PENDING)
                for p, record in records
            )
            if not drift_open and any(p.type in DRIFT_CREATING for p in proposals):
                # Sync may have been paused for a write that was never sent.
                self._resume_sync(incident_id)
            self._finish(
                incident_id, result, S.ESCALATED,
                f"Heimdall restarted while the incident was {state}; no unverified write "
                "was left behind, and the plan was not resumed",
            )  # fmt: skip
            return

        proposal = unverified[-1]
        if self._manager.get(incident_id).state is S.EXECUTING:
            self._move(incident_id, S.VERIFYING, f"restart: verifying {describe(proposal)}")
        verification = self._verifier.verify(
            recovery_checks(proposal, self._manager.get(incident_id))
        )
        self._audit.append(
            EventType.VERIFICATION,
            {
                "action": describe(proposal),
                "recovered": verification.recovered,
                "seconds": verification.seconds,
                "checks": verification.checks,
                "after_restart": True,
            },
            actor="verifier",
            incident_id=incident_id,
        )
        if verification.recovered:
            self._executor.confirm(incident_id, proposal)
            summary = (
                f"Heimdall restarted during {describe(proposal)}. The service is healthy, so "
                "the change was kept. The rest of the plan was not run"
            )
        else:
            undo = self._executor.undo(incident_id, proposal)
            if undo.status is ActionStatus.UNDONE and proposal.type in DRIFT_CREATING:
                self._resume_sync(incident_id)
            self._move(incident_id, S.ROLLED_BACK, f"restart: {describe(proposal)} did not help")
            result.rolled_back = True
            undone = (
                "undone" if undo.status is ActionStatus.UNDONE else f"UNDO FAILED: {undo.detail}"
            )
            summary = (
                f"Heimdall restarted during {describe(proposal)}. The service did not recover "
                f"({', '.join(verification.failing)}); {undone}"
            )
        self._finish(incident_id, result, S.ESCALATED, summary)

    # ------------------------------------------------------------ gitops

    def _resume_sync(self, incident_id: str) -> None:
        if self._gitops is None:
            return
        try:
            self._gitops.resume_sync()
        except (GitOpsError, KubeError) as e:
            self._notifier.send(
                f"Action needed: {incident_id}",
                f"Argo CD automatic sync is still paused and could not be resumed: {e}",
            )

    def reconcile_merged(self, actions: ActionStore) -> list[str]:
        """Close mitigated incidents whose pull request a human has merged:
        git and the cluster agree again, so sync is resumed and the incident
        is resolved. Returns the ids of the incidents that were closed."""
        if self._gitops is None:
            return []
        closed = []
        for incident in self._manager.all(active_only=True):
            if incident.state is not S.MITIGATED:
                continue
            numbers = [
                a.previous["pull_request"]
                for a in actions.for_incident(incident.id)
                if a.status is ActionStatus.PR_OPENED and a.previous
            ]
            try:
                merged = bool(numbers) and all(self._gitops.merged(n) for n in numbers)
            except GitOpsError:
                continue
            if merged:
                self._resume_sync(incident.id)
                self._finish(
                    incident.id, Remediation(), S.RESOLVED,
                    "the pull request was merged; git and the cluster agree again",
                )  # fmt: skip
                closed.append(incident.id)
        return closed

    # ---------------------------------------------------------- helpers

    def _finish(
        self, incident_id: str, result: Remediation, state: IncidentState, summary: str
    ) -> Remediation:
        result.final_state, result.summary = state, summary
        self._move(incident_id, state, summary)
        self._notify(incident_id, result)
        return result

    def _move(self, incident_id: str, to: IncidentState, reason: str) -> None:
        current = self._manager.get(incident_id).state
        if current is to:
            return
        if to not in TRANSITIONS[current] and current is S.VERIFYING:
            # A verified step is followed by more of the plan.
            self._manager.transition(incident_id, S.PLANNING, reason="continuing the plan")
        self._manager.transition(incident_id, to, reason=reason)

    def _recall(self, incident_id: str) -> tuple[Diagnosis | None, list[ActionProposal]]:
        """The validated diagnosis and the plan, as recorded in the audit log."""
        diagnosis, proposals = None, []
        for event in self._audit.events(incident_id):
            if event.event_type == EventType.DIAGNOSIS:
                diagnosis = Diagnosis.model_validate(event.payload["diagnosis"])
            elif event.event_type == EventType.PROPOSAL:
                proposals = [ActionProposal.model_validate(p) for p in event.payload["proposals"]]
        return diagnosis, proposals

    def _notify(self, incident_id: str, result: Remediation) -> None:
        # Summaries and justifications carry text a model wrote. It goes to a
        # chat service, so it is redacted like everything else that leaves.
        if result.awaiting is not None and self._approvals is not None:
            approval = result.awaiting
            url = f"{self._public_url}/approvals/{approval.id}"
            token = self._approvals.token(approval)
            reasons = "; ".join(result.steps[-1][1].reasons)
            self._notifier.send(
                f"Approval needed: {incident_id}",
                f"Heimdall wants to run: {describe(approval.proposal)}\n"
                f"Why it needs you: {reasons}\n"
                f"Why it wants to: {redact(approval.proposal.justification)}\n"
                f"Expires: {approval.expires_at:%H:%M:%S} UTC\n"
                f"Details: {url}?token={token}\n"
                f"To decide, POST to {url} with "
                f'{{"token": "{token}", "decision": "approve"}} (or "reject").',
            )
        elif result.final_state is S.ESCALATED:
            self._notifier.send(f"Escalated: {incident_id}", redact(result.summary))
        elif result.final_state in (S.MITIGATED, S.RESOLVED):
            self._notifier.send(
                f"{result.final_state.capitalize()}: {incident_id}", redact(result.summary)
            )
