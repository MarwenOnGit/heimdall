"""Heimdall is killed at each stage of an action and started again.

Whatever the moment, the restart must never apply a write a second time and
must always end the incident in a safe terminal state.
"""

from pathlib import Path

import pytest

from heimdall.core.actions import ActionStatus
from heimdall.core.models import ActionProposal, IncidentState
from tests.unit.test_pipeline import OPA, World, proposal, release_world, repo_with_bad_release

pytestmark = pytest.mark.skipif(not OPA.exists(), reason="run infra/tools.sh to download opa")

SCALE = proposal("scale_deployment", replicas=3)


class Killed(BaseException):
    """The process dies here. Not an Exception: nothing may catch and tidy up."""


def die(*args, **kwargs):
    raise Killed


def crash(world: World, diagnosis) -> None:
    with pytest.raises(Killed):
        world.pipeline.remediate(diagnosis)


def restart(tmp_path: Path, recovers: list[bool], **kwargs) -> World:
    """A new process: new objects, the same database."""
    return World(tmp_path, [SCALE], live=True, recovers=recovers, **kwargs)


def record(world: World, incident_id: str, raw: dict = SCALE):
    return world.actions.find(incident_id, ActionProposal.model_validate(raw))


def states(world: World, incident_id: str) -> list[str]:
    return [e.payload["to"] for e in world.audit.events(incident_id)
            if e.event_type == "state_transition"]  # fmt: skip


def test_killed_before_the_write(tmp_path: Path) -> None:
    world = World(tmp_path, [SCALE], live=True)
    diagnosis = world.diagnosed("resource_saturation", "frontend")
    world.pipeline._executor.execute = die
    crash(world, diagnosis)
    assert world.state(diagnosis) is IncidentState.EXECUTING and world.kube.patches == []

    after = restart(tmp_path, [])
    assert after.pipeline.recover() == [diagnosis.incident_id]

    assert after.kube.patches == []  # nothing is applied on restart
    assert after.verifier.checked == []
    assert after.manager.get(diagnosis.incident_id).state is IncidentState.ESCALATED
    assert "restarted" in after.sent[-1][1]


def kill_right_after_the_write(world: World) -> None:
    """The request reaches the API server; Heimdall dies before recording it."""
    real = world.actions.record

    def record_or_die(incident_id, proposal, **fields):
        if fields["status"] is ActionStatus.EXECUTED:
            raise Killed
        return real(incident_id, proposal, **fields)

    world.actions.record = record_or_die


def test_killed_after_the_write_and_the_fix_worked(tmp_path: Path) -> None:
    world = World(tmp_path, [SCALE], live=True)
    diagnosis = world.diagnosed("resource_saturation", "frontend")
    kill_right_after_the_write(world)
    crash(world, diagnosis)
    assert len(world.kube.patches) == 1
    assert record(world, diagnosis.incident_id).status is ActionStatus.PENDING

    after = restart(tmp_path, [True])
    after.pipeline.recover()

    assert after.kube.patches == []  # not applied again, not undone
    assert len(after.verifier.checked) == 1
    assert record(after, diagnosis.incident_id).status is ActionStatus.EXECUTED
    assert after.manager.get(diagnosis.incident_id).state is IncidentState.ESCALATED
    assert "was kept" in after.sent[-1][1]


def test_killed_after_the_write_and_the_fix_did_not_work(tmp_path: Path) -> None:
    world = World(tmp_path, [SCALE], live=True)
    diagnosis = world.diagnosed("resource_saturation", "frontend")
    kill_right_after_the_write(world)
    crash(world, diagnosis)

    after = restart(tmp_path, [False])
    after.pipeline.recover()

    (undo,) = after.kube.patches
    assert undo["body"]["spec"] == {"replicas": 1}  # the state saved before the write
    assert record(after, diagnosis.incident_id).status is ActionStatus.UNDONE
    assert states(after, diagnosis.incident_id)[-3:] == ["verifying", "rolled_back", "escalated"]


def test_killed_during_verification(tmp_path: Path) -> None:
    world = World(tmp_path, [SCALE], live=True)
    diagnosis = world.diagnosed("resource_saturation", "frontend")
    world.verifier.verify = die
    crash(world, diagnosis)
    assert world.state(diagnosis) is IncidentState.VERIFYING
    assert record(world, diagnosis.incident_id).status is ActionStatus.EXECUTED

    after = restart(tmp_path, [False])
    after.pipeline.recover()

    (undo,) = after.kube.patches
    assert undo["body"]["spec"] == {"replicas": 1}
    assert after.manager.get(diagnosis.incident_id).state is IncidentState.ESCALATED
    events = [e for e in after.audit.events(diagnosis.incident_id)
              if e.event_type == "verification"]  # fmt: skip
    assert events[-1].payload["after_restart"] is True


def test_killed_between_pausing_sync_and_the_write(tmp_path: Path) -> None:
    """Sync must not stay paused for a write that never happened."""
    gitea = repo_with_bad_release()
    world, diagnosis = release_world(tmp_path, gitea, live=True, recovers=[])
    real = world.actions.record

    def die_at_intent(incident_id, proposal, **fields):
        if fields["status"] is ActionStatus.PENDING:
            raise Killed
        return real(incident_id, proposal, **fields)

    world.actions.record = die_at_intent
    crash(world, diagnosis)
    paused = [p for p in world.kube.patches if "argoproj.io" in p["path"]]
    assert len(paused) == 1 and len(world.kube.patches) == 1  # paused, nothing written

    after = World(tmp_path, [], live=True, gitea=gitea)
    assert after.pipeline.recover() == [diagnosis.incident_id]

    resumed = [p for p in after.kube.patches if "argoproj.io" in p["path"]]
    assert len(resumed) == 1 and len(after.kube.patches) == 1
    assert resumed[0]["body"]["spec"]["syncPolicy"]["automated"] is not None
    assert after.manager.get(diagnosis.incident_id).state is IncidentState.ESCALATED


def test_recovery_runs_once_and_leaves_resting_incidents_alone(tmp_path: Path) -> None:
    world = World(tmp_path, [proposal("scale_deployment", name="redis-cart", replicas=1)],
                  live=True, approvals=True)  # fmt: skip
    waiting = world.diagnosed("dependency_down", "redis-cart")
    world.pipeline.remediate(waiting)
    assert world.state(waiting) is IncidentState.AWAITING_APPROVAL

    interrupted = world.diagnosed("resource_saturation", "frontend")  # left in DIAGNOSED

    after = restart(tmp_path, [], approvals=True)
    assert after.pipeline.recover() == [interrupted.incident_id]
    assert after.pipeline.recover() == []
    assert after.manager.get(waiting.incident_id).state is IncidentState.AWAITING_APPROVAL
    assert after.kube.patches == []
