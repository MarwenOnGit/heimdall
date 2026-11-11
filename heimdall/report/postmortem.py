"""Postmortems, built from the audit log and nothing else.

The audit log is the one record that is hash-chained and anchored, so a report
derived only from it can be checked: every line of the timeline carries the
`seq` of the event it came from. No model writes any part of the report. The
text that did come from a model or a workload (summaries, quoted tool output,
alert labels) is redacted again and escaped before it is rendered.
"""

import html
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from heimdall.agent.pipeline import describe
from heimdall.core.audit import AuditEvent, EventType
from heimdall.core.models import (
    ActionProposal,
    ActionType,
    Diagnosis,
    IncidentState,
    RootCause,
    RuledOut,
)
from heimdall.tools.redact import redact

S = IncidentState
_DRIFT_CREATING = {ActionType.ROLLBACK_DEPLOYMENT, ActionType.PATCH_RESOURCES}
_SEVERITY_ORDER = ["critical", "warning", "info"]


class AlertEntry(BaseModel):
    seq: int
    time: datetime
    alertname: str
    service: str | None = None
    severity: str | None = None


class TimelineEntry(BaseModel):
    seq: int
    time: datetime
    event: str
    text: str


class ActionEntry(BaseModel):
    seq: int
    action: str
    type: ActionType
    justification: str = ""
    decision: str
    reasons: list[str] = []
    status: str = "not run"
    admission: str = "not sent to the API server"
    detail: str = ""
    verification: str | None = None
    undone: bool = False


class Stats(BaseModel):
    tool_calls: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_seconds: float = 0.0
    models: list[str] = []
    rejected_drafts: int = 0


class Postmortem(BaseModel):
    incident_id: str
    state: IncidentState
    severity: str
    services: list[str]
    alerts: list[AlertEntry]
    opened_at: datetime
    diagnosed_at: datetime | None = None
    mitigated_at: datetime | None = None
    closed_at: datetime | None = None
    last_reason: str = ""
    dry_run: bool = False
    root_causes: list[RootCause] = []
    ruled_out: list[RuledOut] = []
    stopped_because: str = ""
    actions: list[ActionEntry] = []
    dropped_proposals: list[str] = []
    drift: str = "none: no action changed what git describes"
    pull_requests: list[str] = []
    follow_ups: list[str] = []
    timeline: list[TimelineEntry] = []
    stats: Stats = Stats()
    last_seq: int

    @property
    def title(self) -> str:
        causes = ", ".join(
            f"{c.category} on {c.affected_resource.name}" if c.affected_resource else c.category
            for c in self.root_causes
        )
        return f"{', '.join(self.services) or 'cluster'}: {causes or 'not diagnosed'}"

    def seconds_to(self, moment: datetime | None) -> float | None:
        return None if moment is None else round((moment - self.opened_at).total_seconds(), 1)


def _clean(value: Any) -> Any:
    """Redact every string in a JSON-like value."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    return value


def _admission(status: str, detail: str) -> str:
    return {
        "executed": "accepted",
        "undone": "accepted",
        "dry_run": "accepted (server-side dry run, nothing changed)",
        "rejected_by_api": f"rejected: {detail}",
        "conflict": "not applied: the target changed after the policy check",
        "failed": "failed before or at the API server",
    }.get(status, "not sent to the API server")


def build(incident_id: str, events: Iterable[AuditEvent]) -> Postmortem:
    """Build the report for one incident from its audit events.

    Raises KeyError when the log holds nothing for that incident.
    """
    events = [e for e in events if e.incident_id == incident_id]
    if not events:
        raise KeyError(incident_id)

    state = S.OPEN
    services: list[str] = []
    alerts: dict[str, AlertEntry] = {}
    timeline: list[TimelineEntry] = []
    actions: list[ActionEntry] = []
    stats = Stats()
    diagnosis: Diagnosis | None = None
    out: dict[str, Any] = {"dry_run": False}
    opened_at = datetime.fromisoformat(events[0].timestamp)
    dropped: list[str] = []
    pull_requests: list[str] = []
    undo_failed = False

    def note(event: AuditEvent, text: str) -> None:
        timeline.append(
            TimelineEntry(
                seq=event.seq,
                time=datetime.fromisoformat(event.timestamp),
                event=event.event_type,
                text=text,
            )
        )

    def last_action(name: str) -> ActionEntry | None:
        return next((a for a in reversed(actions) if a.action == name), None)

    for event in events:
        kind, p = event.event_type, _clean(event.payload)
        when = datetime.fromisoformat(event.timestamp)

        if kind == EventType.ALERT_RECEIVED:
            labels = p.get("labels", {})
            service = labels.get("service")
            if service and service not in services:
                services.append(service)
            if p.get("status") == "firing":
                alerts.setdefault(
                    p.get("fingerprint", str(event.seq)),
                    AlertEntry(
                        seq=event.seq,
                        time=when,
                        alertname=p.get("alertname", ""),
                        service=service,
                        severity=labels.get("severity"),
                    ),
                )
            note(event, f"alert {p.get('alertname')} {p.get('status')} ({service or 'no service'})")

        elif kind == EventType.STATE_TRANSITION:
            state = S(p["to"])
            out["last_reason"] = p.get("reason", "")
            if state is S.MITIGATED:
                out.setdefault("mitigated_at", when)
            if state in (S.RESOLVED, S.ESCALATED, S.FAILED):
                out["closed_at"] = when
            note(event, f"{p.get('from') or 'new'} -> {state}: {p.get('reason', '')}")

        elif kind == EventType.TOOL_CALL:
            stats.tool_calls += 1

        elif kind == EventType.LLM_CALL:
            stats.llm_calls += 1
            stats.prompt_tokens += p.get("prompt_tokens", 0)
            stats.cached_tokens += p.get("cached_tokens", 0)
            stats.completion_tokens += p.get("completion_tokens", 0)
            stats.cost_usd += p.get("cost_usd", 0.0)
            stats.llm_seconds += p.get("latency_seconds", 0.0)
            if p.get("model") and p["model"] not in stats.models:
                stats.models.append(p["model"])

        elif kind == EventType.DIAGNOSIS_REJECTED:
            stats.rejected_drafts += 1
            note(event, "draft diagnosis rejected: " + "; ".join(p.get("problems", []))[:300])

        elif kind == EventType.DIAGNOSIS:
            diagnosis = Diagnosis.model_validate(p["diagnosis"])
            out["diagnosed_at"] = when
            out["stopped_because"] = p.get("stopped_because", "")
            causes = ", ".join(f"{c.category} ({c.confidence:.2f})" for c in diagnosis.root_causes)
            note(event, f"diagnosis: {causes}")

        elif kind == EventType.PROPOSAL:
            plan = [describe(ActionProposal.model_validate(x)) for x in p.get("proposals", [])]
            dropped = [
                f"{describe(ActionProposal.model_validate(d['proposal']))}: {d['reason']}"
                for d in p.get("dropped", [])
            ]
            forced = (
                " (FORCED by the benchmark harness, planner bypassed)" if p.get("forced") else ""
            )
            note(event, "plan" + forced + ": " + ("; ".join(plan) or "nothing"))

        elif kind == EventType.POLICY_DECISION:
            proposal = ActionProposal.model_validate(p["proposal"])
            actions.append(
                ActionEntry(
                    seq=event.seq,
                    action=describe(proposal),
                    type=proposal.type,
                    justification=proposal.justification,
                    decision=p["decision"],
                    reasons=p.get("reasons", []),
                )
            )
            reasons = f" ({'; '.join(p['reasons'])})" if p.get("reasons") else ""
            note(event, f"policy: {p['decision']} for {describe(proposal)}{reasons}")

        elif kind == EventType.APPROVAL:
            verdict = "expired" if p.get("expired") else (
                "approved" if p.get("approved") else "rejected"
            )  # fmt: skip
            note(event, f"approval {verdict} by {event.actor}: {p.get('action', '')}".rstrip(": "))

        elif kind == EventType.ACTION_EXECUTED:
            proposal = ActionProposal.model_validate(p["proposal"])
            out["dry_run"] = out["dry_run"] or bool(p.get("dry_run"))
            entry = last_action(describe(proposal))
            if entry is not None:
                entry.status = p["status"]
                entry.detail = p.get("detail", "")
                if proposal.type not in (ActionType.OPEN_PR, ActionType.ESCALATE, ActionType.NONE):
                    entry.admission = _admission(p["status"], entry.detail)
            if p["status"] == "pr_opened":
                pull_requests.append(p.get("detail", ""))
            note(event, f"{describe(proposal)}: {p['status']} ({p.get('detail', '')})")

        elif kind == EventType.VERIFICATION:
            entry = last_action(p.get("action", ""))
            failing = [name for name, ok in p.get("checks", {}).items() if not ok]
            text = (
                f"recovered after {p.get('seconds')}s"
                if p.get("recovered")
                else f"not recovered after {p.get('seconds')}s ({', '.join(failing)})"
            )
            if entry is not None:
                entry.verification = text
            note(event, f"verification of {p.get('action')}: {text}")

        elif kind == EventType.ROLLBACK:
            proposal = ActionProposal.model_validate(p["proposal"])
            entry = last_action(describe(proposal))
            ok = p.get("status") == "undone"
            undo_failed = undo_failed or not ok
            if entry is not None:
                entry.undone = ok
                entry.status = "undone" if ok else "undo failed"
            note(event, f"undo of {describe(proposal)}: {p.get('status')} ({p.get('detail', '')})")

        elif kind == EventType.ESCALATION:
            note(event, f"escalated: {p.get('summary', '')}")

    severities = [a.severity for a in alerts.values() if a.severity]
    severity = min(
        severities,
        key=lambda s: _SEVERITY_ORDER.index(s) if s in _SEVERITY_ORDER else len(_SEVERITY_ORDER),
        default="unknown",
    )

    drifted = [
        a for a in actions if a.type in _DRIFT_CREATING and a.status == "executed" and not a.undone
    ]
    if drifted and state is S.RESOLVED:
        drift = "reconciled: the pull request was merged and automatic sync is on again"
    elif drifted:
        drift = (
            "the cluster differs from git on purpose; Argo CD automatic sync is paused until "
            "the reconciling pull request is merged"
        )
    elif any(a.type in _DRIFT_CREATING and a.undone for a in actions):
        drift = "none: the action was undone, so the cluster matches git again"
    else:
        drift = "none: no action changed what git describes"

    follow_ups: list[str] = []
    if undo_failed:
        follow_ups.append(
            "An undo FAILED. Check the target by hand: it may still carry Heimdall's change."
        )
    if state is S.ESCALATED:
        follow_ups.append(f"A human has to take over: {out.get('last_reason', '')}")
    if drifted and state is not S.RESOLVED:
        where = ", ".join(pull_requests) or "no pull request was opened; fix git by hand"
        follow_ups.append(f"Review and merge the reconciling pull request ({where}).")
    if out["dry_run"]:
        follow_ups.append(
            "This was a dry run: nothing was changed. The actions show what live mode would do."
        )
    if stats.rejected_drafts:
        follow_ups.append(
            f"{stats.rejected_drafts} draft diagnosis(es) cited evidence that did not check out; "
            "worth a look when tuning the prompt."
        )
    if diagnosis is not None and any(c.category == "unknown" for c in diagnosis.root_causes):
        follow_ups.append("No root cause was established. Investigate by hand.")

    stats.cost_usd = round(stats.cost_usd, 6)
    stats.llm_seconds = round(stats.llm_seconds, 2)
    return Postmortem(
        incident_id=incident_id,
        state=state,
        severity=severity,
        services=services,
        alerts=sorted(alerts.values(), key=lambda a: a.seq),
        opened_at=opened_at,
        root_causes=diagnosis.root_causes if diagnosis else [],
        ruled_out=diagnosis.ruled_out if diagnosis else [],
        actions=actions,
        dropped_proposals=dropped,
        drift=drift,
        pull_requests=pull_requests,
        follow_ups=follow_ups,
        timeline=timeline,
        stats=stats,
        last_seq=events[-1].seq,
        **out,
    )


# ------------------------------------------------------------------ rendering

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|#])")


def _md(text: object) -> str:
    """Untrusted text as inert Markdown on one line."""
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text).split()))


def _code(text: str) -> str:
    """A verbatim quote as a code span, whatever backticks it contains."""
    text = " ".join(text.split())
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    return f"{fence} {text} {fence}"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest:02d}s" if minutes else f"{rest}s"


def _time(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M:%S} UTC" if moment else "-"


def _summary_rows(pm: Postmortem) -> list[tuple[str, str]]:
    return [
        ("Incident", pm.incident_id),
        ("State", f"{pm.state}{' (dry run)' if pm.dry_run else ''}"),
        ("Severity", pm.severity),
        ("Started / detected", _time(pm.opened_at)),
        ("Diagnosed", f"{_time(pm.diagnosed_at)} (+{_duration(pm.seconds_to(pm.diagnosed_at))})"),
        ("Mitigated", f"{_time(pm.mitigated_at)} (+{_duration(pm.seconds_to(pm.mitigated_at))})"),
        ("Closed", f"{_time(pm.closed_at)} (+{_duration(pm.seconds_to(pm.closed_at))})"),
    ]


def _stats_rows(pm: Postmortem) -> list[tuple[str, str]]:
    s = pm.stats
    return [
        ("Tool calls", str(s.tool_calls)),
        ("LLM calls", str(s.llm_calls)),
        ("Models", ", ".join(s.models) or "-"),
        ("Prompt tokens", f"{s.prompt_tokens} ({s.cached_tokens} cached)"),
        ("Completion tokens", str(s.completion_tokens)),
        ("Estimated cost", f"${s.cost_usd:.4f}"),
        ("Time in LLM calls", f"{s.llm_seconds}s"),
        ("Rejected drafts", str(s.rejected_drafts)),
        ("Audit events", f"up to seq {pm.last_seq}"),
    ]


def render_markdown(pm: Postmortem) -> str:
    lines = [f"# Postmortem: {_md(pm.title)}", ""]
    lines += [f"- **{k}:** {_md(v)}" for k, v in _summary_rows(pm)]

    lines += ["", "## Impact", ""]
    lines.append(f"- Affected services: {_md(', '.join(pm.services) or 'none named')}")
    for alert in pm.alerts:
        lines.append(
            f"- {_md(alert.alertname)} on {_md(alert.service or '-')} "
            f"({_md(alert.severity or 'no severity')}), first seen {_time(alert.time)} "
            f"[seq {alert.seq}]"
        )

    lines += ["", "## Timeline", ""]
    lines += [f"- `{e.time:%H:%M:%S}` [seq {e.seq}] {_md(e.text)}" for e in pm.timeline]

    lines += ["", "## Root causes", ""]
    if not pm.root_causes:
        lines.append("No diagnosis was recorded.")
    for index, cause in enumerate(pm.root_causes, start=1):
        ref = cause.affected_resource
        where = f"{ref.kind} {ref.namespace or '-'}/{ref.name}" if ref else "no specific resource"
        lines.append(
            f"{index}. **{cause.category}** on {_md(where)} "
            f"(confidence {cause.confidence:.2f}): {_md(cause.summary)}"
        )

    lines += ["", "## Evidence", ""]
    evidence = [(i, e) for i, c in enumerate(pm.root_causes, start=1) for e in c.evidence]
    if not evidence:
        lines.append("None.")
    for index, item in evidence:
        lines.append(
            f"- Cause {index}, {_md(item.tool_call_id)}: {_code(item.quote)} "
            f"- {_md(item.observation)}"
        )

    lines += ["", "## Hypotheses ruled out", ""]
    if not pm.ruled_out:
        lines.append("None recorded.")
    for ruled in pm.ruled_out:
        cite = f" ({_md(ruled.tool_call_id)})" if ruled.tool_call_id else ""
        lines.append(f"- {_md(ruled.hypothesis)}: {_md(ruled.reason)}{cite}")

    lines += ["", "## Actions", ""]
    if not pm.actions:
        lines.append("No action was proposed.")
    for action in pm.actions:
        lines.append(f"- **{_md(action.action)}** [seq {action.seq}]")
        if action.justification:
            lines.append(f"  - Why: {_md(action.justification)}")
        reasons = f" ({_md('; '.join(action.reasons))})" if action.reasons else ""
        lines.append(f"  - Policy: {action.decision}{reasons}")
        lines.append(f"  - API server: {_md(action.admission)}")
        lines.append(f"  - Outcome: {_md(action.status)} {_md(action.detail)}".rstrip())
        if action.verification:
            lines.append(f"  - Verification: {_md(action.verification)}")
    for dropped in pm.dropped_proposals:
        lines.append(f"- Dropped before the policy gate: {_md(dropped)}")

    lines += ["", "## Drift and pull requests", "", f"- Drift: {_md(pm.drift)}"]
    lines += [f"- Pull request: {_md(url)}" for url in pm.pull_requests]

    lines += ["", "## Follow-ups", ""]
    lines += [f"- {_md(item)}" for item in pm.follow_ups] or ["None."]

    lines += ["", "## Agent stats", ""]
    lines += [f"- {k}: {_md(v)}" for k, v in _stats_rows(pm)]
    return "\n".join(lines) + "\n"


_STYLE = """
body{font:15px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;
color:#1c2024;background:#fff}
h1{font-size:1.5rem}h2{font-size:1.1rem;margin-top:2rem;border-bottom:1px solid #d9dde3}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.25rem .6rem;
border-bottom:1px solid #e6e9ee;vertical-align:top}
code{background:#f1f3f6;padding:.1rem .3rem;border-radius:3px;font-size:.9em}
.seq{color:#6b7280;font-size:.85em;white-space:nowrap}
@media(prefers-color-scheme:dark){body{color:#e6e8eb;background:#14171a}
h2,td,th{border-color:#2c3238}code{background:#22272d}.seq{color:#9aa3ad}}
"""


def render_html(pm: Postmortem) -> str:
    """A self-contained page. Every value is escaped; nothing is fetched."""
    e = html.escape

    def table(rows: Iterable[tuple[str, ...]], head: tuple[str, ...] = ()) -> str:
        out = ["<table>"]
        if head:
            out.append("<tr>" + "".join(f"<th>{e(h)}</th>" for h in head) + "</tr>")
        for row in rows:
            out.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
        return "\n".join([*out, "</table>"])

    def items(values: Iterable[str], empty: str) -> str:
        values = list(values)
        if not values:
            return f"<p>{e(empty)}</p>"
        return "<ul>" + "".join(f"<li>{v}</li>" for v in values) + "</ul>"

    def seq(n: int) -> str:
        return f'<span class="seq">seq {n}</span>'

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>Postmortem {e(pm.incident_id)}</title><style>{_STYLE}</style></head><body>",
        f"<h1>Postmortem: {e(pm.title)}</h1>",
        table((e(k), e(v)) for k, v in _summary_rows(pm)),
        "<h2>Impact</h2>",
        f"<p>Affected services: {e(', '.join(pm.services) or 'none named')}</p>",
        table(
            (
                (
                    e(a.alertname),
                    e(a.service or "-"),
                    e(a.severity or "-"),
                    e(_time(a.time)),
                    seq(a.seq),
                )
                for a in pm.alerts
            ),
            ("Alert", "Service", "Severity", "First seen", ""),
        ),  # fmt: skip
        "<h2>Timeline</h2>",
        table(
            ((e(f"{t.time:%H:%M:%S}"), seq(t.seq), e(t.event), e(t.text)) for t in pm.timeline),
            ("Time", "", "Event", "What happened"),
        ),
        "<h2>Root causes</h2>",
    ]
    causes = []
    for cause in pm.root_causes:
        ref = cause.affected_resource
        where = f"{ref.kind} {ref.namespace or '-'}/{ref.name}" if ref else "no specific resource"
        causes.append(
            f"<strong>{e(cause.category)}</strong> on {e(where)} "
            f"(confidence {cause.confidence:.2f}): {e(cause.summary)}"
        )
    parts.append(items(causes, "No diagnosis was recorded."))
    parts.append("<h2>Evidence</h2>")
    parts.append(
        table(
            (
                (str(i), e(item.tool_call_id), f"<code>{e(item.quote)}</code>", e(item.observation))
                for i, c in enumerate(pm.root_causes, start=1)
                for item in c.evidence
            ),
            ("Cause", "Tool call", "Quote", "Observation"),
        )  # fmt: skip
    )
    parts.append("<h2>Hypotheses ruled out</h2>")
    parts.append(
        items(
            (
                f"{e(r.hypothesis)}: {e(r.reason)}"
                + (f" ({e(r.tool_call_id)})" if r.tool_call_id else "")
                for r in pm.ruled_out
            ),
            "None recorded.",
        )
    )
    parts.append("<h2>Actions</h2>")
    parts.append(
        table(
            (
                (
                    f"{e(a.action)} {seq(a.seq)}",
                    e(a.decision + (f" ({'; '.join(a.reasons)})" if a.reasons else "")),
                    e(a.admission),
                    e(f"{a.status} {a.detail}".strip()),
                    e(a.verification or "-"),
                )
                for a in pm.actions
            ),
            ("Action", "Policy", "API server", "Outcome", "Verification"),
        )
    )
    if pm.dropped_proposals:
        parts.append("<p>Dropped before the policy gate:</p>")
        parts.append(items((e(d) for d in pm.dropped_proposals), ""))
    parts.append("<h2>Drift and pull requests</h2>")
    parts.append(items([e(f"Drift: {pm.drift}"), *(e(u) for u in pm.pull_requests)], ""))
    parts.append("<h2>Follow-ups</h2>")
    parts.append(items((e(f) for f in pm.follow_ups), "None."))
    parts.append("<h2>Agent stats</h2>")
    parts.append(table((e(k), e(v)) for k, v in _stats_rows(pm)))
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"
