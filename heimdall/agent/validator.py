"""Checks a diagnosis against what the tools actually returned.

A model can produce a confident, well-formed diagnosis whose evidence it made
up. This validator makes that impossible to act on: every piece of evidence
must cite a real tool call and quote text that really appears in that call's
output.
"""

import re
from collections.abc import Callable

from heimdall.core.models import Diagnosis, ResourceRef, RootCauseCategory
from heimdall.tools.sanitize import FLAG

# These need no proof of a specific broken object.
_NO_RESOURCE = {RootCauseCategory.UNKNOWN, RootCauseCategory.NO_ACTION_NEEDED}

_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text.replace(FLAG, "")).strip()


def validate(
    diagnosis: Diagnosis,
    tool_outputs: dict[str, str],
    *,
    min_evidence: int = 2,
    resource_exists: Callable[[ResourceRef], bool] | None = None,
) -> list[str]:
    """Return every problem found. An empty list means the diagnosis stands.

    `tool_outputs` maps tool call ids to their complete stored output.
    """
    problems: list[str] = []
    normalized = {call_id: _normalize(output) for call_id, output in tool_outputs.items()}

    for index, cause in enumerate(diagnosis.root_causes, start=1):
        where = f"root cause {index} ({cause.category})"
        if cause.category is RootCauseCategory.UNKNOWN:
            continue

        if cause.category not in _NO_RESOURCE:
            if cause.affected_resource is None:
                problems.append(f"{where}: affected_resource is required")
            elif resource_exists is not None and not resource_exists(cause.affected_resource):
                ref = cause.affected_resource
                problems.append(f"{where}: {ref.kind} {ref.name!r} does not exist")

        supported: set[str] = set()
        for item in cause.evidence:
            if item.tool_call_id not in normalized:
                problems.append(
                    f"{where}: evidence cites {item.tool_call_id}, which does not exist"
                )
                continue
            quote = _normalize(item.quote)
            if not quote or quote not in normalized[item.tool_call_id]:
                problems.append(
                    f"{where}: the quote {item.quote[:80]!r} does not appear in the output of "
                    f"{item.tool_call_id}; quotes must be copied exactly"
                )
                continue
            supported.add(item.tool_call_id)

        if len(supported) < min_evidence:
            problems.append(
                f"{where}: needs evidence from at least {min_evidence} different tool calls, "
                f"has {len(supported)} valid"
            )

    for ruled_out in diagnosis.ruled_out:
        if ruled_out.tool_call_id is not None and ruled_out.tool_call_id not in normalized:
            problems.append(f"ruled_out cites {ruled_out.tool_call_id}, which does not exist")
    return problems
