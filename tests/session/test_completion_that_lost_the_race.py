"""A completion that lost the race to another ender announces no reply and is not counted completed (ticket 01a1134b-2cb8).

The clean-completion exit of ``run_one_session_turn`` writes the turn's terminal status through ``_transition_session_status``.
When another path ended the row meanwhile (a force-delete, the pool's preempt convergence, the reconciler) that write is
SKIPPED and the exit computes ``overridden`` from it. The exit announces what the ROW says (``session.ended`` with the
row's reason; the channel relay and the drain honour ``overridden``), and for such a turn it emits no ``session.replied`` (the
durable event log must not say a session replied that it also says was ended by something else) and counts it under its own
``overridden`` status, not ``completed`` (a dashboard that sums completions must not include it, and the turn is still counted
once, so ``turns_total`` over all statuses equals the turns that ran). ``TurnLogCompleted`` is still written, as for any turn
whose model call finished; it is not pinned either way.

Driven through the real ``run_one_session_turn``; the row is ended by another writer between the model's last event and the
turn's terminal write, the way ``test_transition_reports_what_it_left`` injects the same race. Only the event recorder and the
turn counter are spied.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import primer.observability.metrics as metrics
import primer.session.dispatch as dispatch
from primer.model.chat import Done, TextDelta
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.session import test_transition_keeps_the_ended_reason as ended
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)
from tests.session.test_dispatch_interrupt import _build_returning, _StopAwareExecutor

# (ended_reason, cancel_requested): a force-delete sets the cancel flag first; the pool's preempt convergence and the
# reconciler end the row without it.
ENDERS = [
    pytest.param("force_deleted", True, id="force-delete"),
    pytest.param("cancelled", False, id="preempt-convergence"),
    pytest.param("workspace_lost", False, id="reconciler"),
]


class _Run:
    def __init__(self) -> None:
        self.emitted: list[str] = []
        self.completed_before = 0.0
        self.completed_after = 0.0
        self.overridden_before = 0.0
        self.overridden_after = 0.0

    @property
    def completed_delta(self) -> float:
        return self.completed_after - self.completed_before

    @property
    def overridden_delta(self) -> float:
        return self.overridden_after - self.overridden_before


async def _turn(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, *,
    ender: tuple[str, bool] | None,
) -> _Run:
    """Run one clean turn. With ``ender`` set, the row is ended by another writer right after the model's last event."""
    sid = seeded_session.id
    storage = fake_storage_provider.get_storage(WorkspaceSession)
    run = _Run()
    ref = dispatch._binding_ref(await storage.get(sid))
    run.completed_before = metrics.turns_total.labels(ref, "completed")._value.get()
    run.overridden_before = metrics.turns_total.labels(ref, "overridden")._value.get()

    class _Recorder:
        async def emit(self, name: str, **_kwargs: Any) -> None:
            run.emitted.append(name)

    monkeypatch.setattr(dispatch, "_event_recorder", lambda deps: _Recorder())
    if ender is not None:
        reason, cancel_flag = ender

        async def read_status_while_the_other_ender_lands(executor: Any):
            row = await storage.get(sid)
            row.status, row.ended_reason, row.cancel_requested = SessionStatus.ENDED, reason, cancel_flag
            await storage.update(row)
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_while_the_other_ender_lands)
    executor = _StopAwareExecutor([TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop")])
    executor.session = ended._Slot()
    deps = SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
        build_executor=_build_returning(executor),
    )

    await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 5.0)

    run.completed_after = metrics.turns_total.labels(ref, "completed")._value.get()
    run.overridden_after = metrics.turns_total.labels(ref, "overridden")._value.get()
    return run


async def test_control_a_turn_nobody_ended_replies_and_counts_completed(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
) -> None:
    """The spies see what they must: with no other ender the same turn emits ``session.replied`` and is counted completed,
    so the absence asserted below is the fix's doing and not a blind spot of this harness."""
    run = await _turn(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, ender=None,
    )

    assert "session.replied" in run.emitted
    assert run.completed_delta == 1.0
    assert run.overridden_delta == 0.0


@pytest.mark.parametrize("reason, cancel_flag", ENDERS)
async def test_scenario_another_ender_won_the_race_and_the_turn_announced_the_rows_outcome(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, reason, cancel_flag,
) -> None:
    run = await _turn(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, ender=(reason, cancel_flag),
    )

    row = await fake_storage_provider.get_storage(WorkspaceSession).get(seeded_session.id)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, reason), "the turn's write must not overwrite the ender's"
    assert "session.ended" in run.emitted, "the turn announces what the ROW says"


@pytest.mark.parametrize("reason, cancel_flag", ENDERS)
async def test_a_completion_that_lost_the_race_announces_no_reply_and_is_not_counted_completed(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, reason, cancel_flag,
) -> None:
    run = await _turn(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, ender=(reason, cancel_flag),
    )

    problems = []
    if "session.replied" in run.emitted:
        problems.append(f"session.replied was emitted for a session the row says ended/{reason}: {run.emitted}")
    if run.completed_delta != 0.0:
        problems.append(f"the turn was counted completed ({run.completed_delta}) for a session the row says ended/{reason}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("reason, cancel_flag", ENDERS)
async def test_a_completion_that_lost_the_race_is_counted_once_under_overridden(
    seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, reason, cancel_flag,
) -> None:
    run = await _turn(
        seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, ender=(reason, cancel_flag),
    )

    assert run.overridden_delta == 1.0, "the turn ran and is counted, once, under its own status"
    assert run.completed_delta == 0.0
