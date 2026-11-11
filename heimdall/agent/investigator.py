"""The investigator: an LLM loop that diagnoses an incident with read-only tools.

The loop is hand-written rather than built on an agent framework so that every
step is visible, audited and replayable. The model never acts: it asks for
tool calls and finally produces a diagnosis, which is only accepted if its
evidence checks out against the real tool output.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

from heimdall.agent.validator import validate
from heimdall.core.audit import AuditLog, EventType
from heimdall.core.models import (
    Diagnosis,
    Incident,
    ResourceRef,
    RootCause,
    RootCauseCategory,
    RuledOut,
)
from heimdall.llm.base import LLM, LLMError, Message
from heimdall.tools.registry import ToolRegistry

PROMPT_PATH = Path(__file__).parents[1] / "llm" / "prompts" / "investigator.md"


class Hypothesis(BaseModel):
    cause: str
    probability: float = Field(ge=0, le=1)


class ToolRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


class DiagnosisDraft(BaseModel):
    root_causes: list[RootCause] = Field(min_length=1)
    ruled_out: list[RuledOut] = []


class Step(BaseModel):
    """What the model returns at every turn."""

    hypotheses: list[Hypothesis] = []
    tool: ToolRequest | None = None
    diagnosis: DiagnosisDraft | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Step":
        if (self.tool is None) == (self.diagnosis is None):
            raise ValueError("set exactly one of `tool` and `diagnosis`")
        return self


@dataclass(frozen=True)
class Budget:
    max_tool_calls: int = 15
    max_seconds: float = 240
    max_tokens: int = 200_000
    max_rejections: int = 2
    max_invalid_replies: int = 2
    min_evidence: int = 2


@dataclass
class Investigation:
    diagnosis: Diagnosis
    tool_calls: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    rejections: int = 0
    stopped_because: str = "diagnosed"
    transcript: list[Message] = field(default_factory=list)


def load_prompt() -> tuple[str, str]:
    """Return the prompt template and its version (a hash of its content)."""
    text = PROMPT_PATH.read_text()
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]


def unknown(incident_id: str, reason: str) -> Diagnosis:
    return Diagnosis(
        incident_id=incident_id,
        root_causes=[RootCause(category=RootCauseCategory.UNKNOWN, summary=reason, confidence=0.0)],
    )


_NOT_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _name(label: str) -> str:
    """A label value as it may appear in the prompt outside a data block:
    only the characters a Kubernetes name can have, so it cannot carry text."""
    return _NOT_NAME.sub("?", label)[:63]


class Investigator:
    def __init__(
        self,
        llm: LLM,
        registry: ToolRegistry,
        audit: AuditLog,
        *,
        budget: Budget | None = None,
        first_look: Callable[[Incident], list[tuple[str, dict[str, Any]]]] | None = None,
        resource_exists: Callable[[ResourceRef], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        not_evidence: frozenset[str] = frozenset(),
    ) -> None:
        # Names of tools whose output may guide the search but not back a diagnosis.
        self._not_evidence = not_evidence
        self._llm = llm
        self._registry = registry
        self._audit = audit
        self._budget = budget or Budget()
        self._first_look = first_look or (lambda incident: [])
        self._resource_exists = resource_exists
        self._clock = clock
        template, self.prompt_version = load_prompt()
        tools = "\n\n".join(
            f"### {t['name']}\n{t['description']}\nArguments (JSON schema): "
            f"{json.dumps(t['input_schema'].get('properties', {}))}"
            for t in registry.schemas()
        )
        self._system = template.format(tools=tools)

    # ------------------------------------------------------------- pieces

    def _ask(self, incident: Incident, run: Investigation) -> Step | None:
        """One model turn. Returns None when the reply is not a usable Step;
        in that case a correction request has been added to the transcript."""
        response = self._llm.complete(run.transcript, Step.model_json_schema())
        run.llm_calls += 1
        run.prompt_tokens += response.prompt_tokens
        run.completion_tokens += response.completion_tokens
        self._audit.append(
            EventType.LLM_CALL,
            {
                "model": response.model,
                "prompt_version": self.prompt_version,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "cached_tokens": response.cached_tokens,
                "latency_seconds": response.latency_seconds,
                "cost_usd": response.cost_usd,
                "reply": response.content,
            },
            actor="investigator",
            incident_id=incident.id,
        )
        run.transcript.append(Message(role="assistant", content=response.content))
        try:
            return Step.model_validate_json(response.content)
        except ValidationError as e:
            problems = "; ".join(err["msg"] for err in e.errors()[:5])
            run.transcript.append(
                Message(
                    role="user",
                    content=f"Your reply was not valid: {problems}. Reply again with one JSON "
                    "object in the required format.",
                )
            )
            return None

    def _call_tool(
        self, incident: Incident, run: Investigation, name: str, arguments: dict[str, Any]
    ) -> str:
        result = self._registry.call(incident.id, name, arguments)
        run.tool_calls += 1
        return result.for_model

    def _tool_outputs(self, incident_id: str) -> dict[str, str]:
        return {
            e.payload["call_id"]: e.payload["output"]
            for e in self._audit.events(incident_id)
            if e.event_type == EventType.TOOL_RESULT
        }

    def _uncitable(self, incident_id: str) -> set[str]:
        return {
            e.payload["call_id"]
            for e in self._audit.events(incident_id)
            if e.event_type == EventType.TOOL_RESULT and e.payload["tool"] in self._not_evidence
        }

    def _remaining(self, run: Investigation, started: float) -> str | None:
        """Why the investigation must stop now, or None if it may continue."""
        if run.tool_calls >= self._budget.max_tool_calls:
            return "tool call budget exhausted"
        if self._clock() - started >= self._budget.max_seconds:
            return "time budget exhausted"
        if run.prompt_tokens + run.completion_tokens >= self._budget.max_tokens:
            return "token budget exhausted"
        return None

    # --------------------------------------------------------------- loop

    def investigate(self, incident: Incident, alerts: list[dict[str, Any]]) -> Investigation:
        started = self._clock()
        run = Investigation(diagnosis=unknown(incident.id, "investigation did not finish"))

        opening = [
            f"Incident {incident.id}",
            f"Namespace: {incident.namespace or '(none: node-level alert)'}",
            f"Services with alerts: {', '.join(map(_name, incident.services)) or '(none)'}",
            "Alerts:",
            *(f"- {json.dumps(a, sort_keys=True)}" for a in alerts),
            "",
            "Initial observations, collected automatically:",
        ]
        for name, arguments in self._first_look(incident):
            opening.append(self._call_tool(incident, run, name, arguments))
        opening.append("\nBegin. State your hypotheses and request your first tool call.")
        run.transcript = [
            Message(role="system", content=self._system),
            Message(role="user", content="\n".join(opening)),
        ]

        invalid_replies = 0
        final_attempt = False
        try:
            while True:
                step = self._ask(incident, run)
                if step is None:
                    invalid_replies += 1
                    if invalid_replies > self._budget.max_invalid_replies or final_attempt:
                        run.stopped_because = "the model did not produce a usable reply"
                        break
                    continue

                if step.diagnosis is not None:
                    diagnosis = Diagnosis(incident_id=incident.id, **step.diagnosis.model_dump())
                    problems = validate(
                        diagnosis,
                        self._tool_outputs(incident.id),
                        min_evidence=self._budget.min_evidence,
                        resource_exists=self._resource_exists,
                        not_evidence=self._uncitable(incident.id),
                    )
                    if not problems:
                        run.diagnosis = diagnosis
                        run.stopped_because = "diagnosed"
                        break
                    run.rejections += 1
                    self._audit.append(
                        EventType.DIAGNOSIS_REJECTED,
                        {"problems": problems, "draft": diagnosis.model_dump(mode="json")},
                        actor="validator",
                        incident_id=incident.id,
                    )
                    if run.rejections > self._budget.max_rejections or final_attempt:
                        run.stopped_because = "the diagnosis could not be backed by evidence"
                        break
                    run.transcript.append(
                        Message(
                            role="user",
                            content="Your diagnosis was rejected because its evidence does not "
                            "check out:\n"
                            + "\n".join(f"- {p}" for p in problems)
                            + "\nFix it. Quote the tool output exactly, call more tools if "
                            "you need more evidence, or answer `unknown` if you cannot support "
                            "a cause.",
                        )
                    )
                    continue

                # A tool request.
                exhausted = self._remaining(run, started)
                if exhausted or final_attempt:
                    if final_attempt:
                        run.stopped_because = exhausted or "budget exhausted"
                        break
                    final_attempt = True
                    run.transcript.append(
                        Message(
                            role="user",
                            content=f"No more tool calls: {exhausted}. Give your final diagnosis "
                            "now from the evidence you already have, or category `unknown`.",
                        )
                    )
                    continue
                assert step.tool is not None  # noqa: S101 - guaranteed by Step validation
                output = self._call_tool(incident, run, step.tool.name, step.tool.arguments)
                left = self._budget.max_tool_calls - run.tool_calls
                run.transcript.append(
                    Message(role="user", content=f"{output}\n\nTool calls left: {left}.")
                )
        except LLMError as e:
            run.stopped_because = f"LLM failure: {e}"

        if run.stopped_because != "diagnosed":
            run.diagnosis = unknown(incident.id, run.stopped_because)
        run.seconds = round(self._clock() - started, 2)
        self._audit.append(
            EventType.DIAGNOSIS,
            {
                "diagnosis": run.diagnosis.model_dump(mode="json"),
                "stopped_because": run.stopped_because,
                "tool_calls": run.tool_calls,
                "llm_calls": run.llm_calls,
                "prompt_tokens": run.prompt_tokens,
                "completion_tokens": run.completion_tokens,
                "seconds": run.seconds,
                "rejections": run.rejections,
            },
            actor="investigator",
            incident_id=incident.id,
        )
        return run
