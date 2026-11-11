"""The scenario file format.

A scenario describes one fault: how to inject it, which alerts it must
trigger, what the true root cause is, and which fixes are acceptable. String
fields may contain `{name}` placeholders that are filled from the sampled
parameters, so one scenario file covers many concrete runs.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from heimdall.core.models import ActionProposal, ActionType, ResourceRef, RootCauseCategory


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Choice(_Strict):
    """Pick one value from a list."""

    choice: list[str | int | float] = Field(min_length=1)


class IntRange(_Strict):
    """Pick an integer, both ends included."""

    int_range: tuple[int, int]

    @model_validator(mode="after")
    def _ordered(self) -> "IntRange":
        if self.int_range[0] > self.int_range[1]:
            raise ValueError("int_range must be [low, high] with low <= high")
        return self


Parameter = Choice | IntRange


class GitCommit(_Strict):
    """A commit to the GitOps repo: the way a real team ships a change.

    `files` maps a path in the repo to its new content. `patches` lists files
    (normally ones written by this commit) to register as kustomize patches.
    """

    type: Literal["git_commit"]
    message: str
    files: dict[str, str] = Field(min_length=1)
    patches: list[str] = []


class Kubectl(_Strict):
    """A kubectl command run against the cluster, bypassing git."""

    type: Literal["kubectl"]
    args: list[str] = Field(min_length=1)


class Manifest(_Strict):
    """An object applied directly, such as a Chaos Mesh experiment."""

    type: Literal["manifest"]
    content: str


class PauseNode(_Strict):
    """Freeze a kind node container, which makes the node NotReady."""

    type: Literal["pause_node"]
    node: str


class Wait(_Strict):
    type: Literal["wait"]
    seconds: int = Field(ge=0, le=600)


Step = Annotated[GitCommit | Kubectl | Manifest | PauseNode | Wait, Field(discriminator="type")]


class ExpectedAlert(_Strict):
    alertname: str
    service: str | None = None


class GroundTruth(_Strict):
    category: RootCauseCategory
    resource: ResourceRef | None = None

    @model_validator(mode="after")
    def _resource_required(self) -> "GroundTruth":
        if self.category is not RootCauseCategory.NO_ACTION_NEEDED and self.resource is None:
            raise ValueError("ground truth needs a resource unless nothing is wrong")
        return self


class Scenario(_Strict):
    id: str = Field(pattern=r"^[a-z]\d{2}$")
    description: str
    difficulty: Literal["easy", "medium", "hard"]
    parameters: dict[str, Parameter] = {}
    inject: list[Step] = Field(min_length=1)
    expected_alerts: list[ExpectedAlert]
    # How long the runner waits for the expected alerts after injection.
    alert_timeout_seconds: int = Field(default=480, ge=30, le=1800)
    ground_truth: list[GroundTruth] = Field(min_length=1)
    acceptable_fixes: list[ActionType] = Field(min_length=1)
    # PromQL expressions. Recovery holds when every one returns at least one
    # sample; the runner also requires all pods to be Ready.
    recovery_condition: list[str] = []
    # A plan the harness forces on Heimdall instead of letting it plan, to
    # test what happens after a wrong fix (s15). Needs live mode.
    forced_plan: list[ActionProposal] = []

    def render(self, params: dict[str, Any]) -> "Scenario":
        """Return a copy with every `{placeholder}` filled from `params`."""
        missing = set(self.parameters) - set(params)
        if missing:
            raise ValueError(f"missing parameters: {sorted(missing)}")
        data = self.model_dump(mode="json", exclude={"parameters"})
        return Scenario.model_validate({**_fill(data, params), "parameters": {}})


def _fill(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        try:
            return value.format_map(params)
        except KeyError as e:
            raise ValueError(f"unknown placeholder {{{e.args[0]}}} in {value!r}") from e
    if isinstance(value, list):
        return [_fill(v, params) for v in value]
    if isinstance(value, dict):
        return {_fill(k, params): _fill(v, params) for k, v in value.items()}
    return value


def load_scenario(path: Path) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(path.read_text()))


def load_all(directory: Path) -> list[Scenario]:
    scenarios = [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate scenario ids in {directory}")
    return scenarios
