"""`search_past_incidents`: what happened the last time something like this broke.

What it returns is a hint about where to look, never evidence. A past report
describes another incident, and part of it was written by a model, so the
investigator refuses evidence that cites this tool (see `NOT_EVIDENCE`).
"""

from pydantic import BaseModel, Field

from heimdall.report.store import PostmortemStore, search
from heimdall.tools.registry import Tool

SEARCH_PAST_INCIDENTS = "search_past_incidents"
# Tools whose output may guide an investigation but cannot back a diagnosis.
NOT_EVIDENCE = frozenset({SEARCH_PAST_INCIDENTS})


class SearchArgs(BaseModel):
    query: str = Field(
        min_length=2, description="Symptoms or names, e.g. 'frontend high error rate after sync'"
    )


class HistoryTools:
    def __init__(self, store: PostmortemStore) -> None:
        self._store = store

    def search_past_incidents(self, args: SearchArgs) -> str:
        found = search(self._store, args.query)
        if not found:
            return "no similar past incidents"
        lines = [
            "Past incidents are hints about where to look. They are not evidence for this one."
        ]
        for stored in found:
            pm = stored.postmortem
            lines.append(
                f"incident={pm.incident_id} opened={pm.opened_at:%Y-%m-%d} outcome={pm.state} "
                f"services={','.join(pm.services) or '-'} "
                f"alerts={','.join(sorted({a.alertname for a in pm.alerts})) or '-'}"
            )
            for cause in pm.root_causes:
                where = cause.affected_resource.name if cause.affected_resource else "-"
                lines.append(f"  root_cause={cause.category} resource={where}: {cause.summary}")
            for action in pm.actions:
                lines.append(
                    f"  action={action.action} policy={action.decision} -> {action.status}"
                )
        return "\n".join(lines)

    def tools(self) -> list[Tool]:
        return [
            Tool(SEARCH_PAST_INCIDENTS, "Find up to 3 similar past incidents with their root "
                 "cause and what was done. Use it for ideas about what to check. You cannot "
                 "cite its output as evidence.", SearchArgs, self.search_past_incidents),
        ]  # fmt: skip
