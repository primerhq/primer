"""Stop through ``run_one_session_turn``: it lands, it is never silently lost, and it keeps the output.

What this pins (each had no test before; the one existing Stop test pins only the cooperative form,
where the executor yields an event and THEN the turn stops):

* A Stop reaches an executor that is NOT yielding (a model that has not produced its first token) and
  lands through the existing soft exit: CANCELLED(operator_interrupt), the row WAITING, the flag cleared.
* The executor, not dispatch, owns where the turn stops once it has bound the event: a Stop that arrives
  while a tool batch is being yielded does not cut the batch's results short, so the log stays paired.
* Output the model had already streamed when the Stop landed becomes a durable record before CANCELLED.
  Text is coalesced and only becomes a record at a tool call or at Done, so without this it existed only
  in the live view and vanished on refresh.
* A Stop cannot be lost to the bus. The watcher also polls the session row (the flag the route sets), so a
  publish that failed, a bus that cannot subscribe, a Stop recorded before the turn began and the window
  before the subscription is live are all covered by one mechanism.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

import primer.observability.metrics as metrics
import primer.session.dispatch as dispatch
from primer.agent.interrupt import Interrupted, interruptible
from primer.model.chat import Done, ExtendedEvent, TextDelta, _ExecutorToolResult
from primer.model.workspace_session import SessionMessageKind, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeWorkspaceIO,
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


def _poll_catches(reason: str) -> float:
    return metrics.session_interrupts_via_poll_total.labels(reason)._value.get()


class _StopAwareExecutor:
    """Honours the bound Stop event the way the agent loop does: every wait is wrapped in
    ``interruptible``, and a Stop ends ``invoke`` cleanly with ``was_interrupted`` set.

    ``script`` items: an event to yield, the string ``"BLOCK"`` (wait forever, e.g. a model that has not
    answered), or a callable awaited in place (to run a side effect at that point of the turn)."""

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.event: asyncio.Event | None = None
        self.was_interrupted = False

    def bind_interrupt_event(self, event: asyncio.Event | None) -> None:
        self.event = event

    async def invoke(self, messages: list[Any], **kwargs: Any):
        self.was_interrupted = False
        for item in self.script:
            if item == "BLOCK":
                try:
                    async with interruptible(self.event):
                        await asyncio.Event().wait()
                except Interrupted:
                    self.was_interrupted = True
                    return
            elif callable(item):
                await item()
            else:
                yield item


def _deps(storage, io, bus, executor) -> SessionDispatchDeps:
    async def build(_session: WorkspaceSession):
        return executor

    return SessionDispatchDeps(
        storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=build,
    )


async def _request_stop(storage, bus, session_id: str, *, publish: bool = True) -> None:
    """What POST .../interrupt does for a RUNNING session: flag the row, then publish the bus key."""
    sessions = storage.get_storage(WorkspaceSession)
    row = await sessions.get(session_id)
    row.interrupt_requested = True
    await sessions.update(row)
    if publish:
        await bus.publish(f"session:{session_id}:cancel", {})


def _records(io: FakeWorkspaceIO, session_id: str) -> list[dict]:
    return [json.loads(line) for line in io.read_lines(session_id)]


async def _run(storage, io, bus, executor, session_id: str, *, timeout: float = 3.0):
    lease = _make_lease(session_id)
    return await asyncio.wait_for(run_one_session_turn(lease, _deps(storage, io, bus, executor)), timeout)


class TestStopReachesAnExecutorThatIsNotYielding:
    async def test_a_stop_before_the_first_event_lands_through_the_soft_exit(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        executor = _StopAwareExecutor(["BLOCK"])
        sid = seeded_session.id
        turn = asyncio.create_task(_run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid))

        await asyncio.sleep(0.05)
        await _request_stop(fake_storage_provider, fake_event_bus, sid)
        outcome = await turn

        assert outcome.success is True and outcome.drop_lease is True
        records = _records(fake_workspace_io, sid)
        kinds = [r["kind"] for r in records]
        assert SessionMessageKind.CANCELLED in kinds and SessionMessageKind.DONE not in kinds
        cancelled = next(r for r in records if r["kind"] == SessionMessageKind.CANCELLED)
        assert cancelled["payload"]["reason"] == "operator_interrupt"
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.WAITING and row.ended_at is None
        assert row.interrupt_requested is False

    async def test_output_already_streamed_becomes_a_durable_record_before_cancelled(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        executor = _StopAwareExecutor([TextDelta(text="par", index=0), TextDelta(text="tial", index=0), "BLOCK"])
        sid = seeded_session.id
        turn = asyncio.create_task(_run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid))

        await asyncio.sleep(0.05)
        await _request_stop(fake_storage_provider, fake_event_bus, sid)
        await turn

        records = _records(fake_workspace_io, sid)
        kinds = [r["kind"] for r in records]
        assert kinds.index(SessionMessageKind.ASSISTANT_TOKEN) < kinds.index(SessionMessageKind.CANCELLED)
        token = next(r for r in records if r["kind"] == SessionMessageKind.ASSISTANT_TOKEN)
        assert token["payload"]["text"] == "partial"

    async def test_a_stop_that_arrives_mid_batch_does_not_cut_the_batch_short(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """The executor decides where to stop once it owns the event. Dispatch used to break after the
        FIRST result record of a batch, leaving the rest of the tool_use records unpaired."""

        def result(call_id: str) -> ExtendedEvent:
            return ExtendedEvent(extended=_ExecutorToolResult(call_id=call_id, output="ok", error=False))

        sid = seeded_session.id

        async def stop_lands_now() -> None:
            await _request_stop(fake_storage_provider, fake_event_bus, sid)
            await asyncio.sleep(0.1)                     # the watcher sets the event

        # A TextDelta first: it produces no record (it coalesces), which is the other place dispatch
        # used to check the event and break.
        executor = _StopAwareExecutor([
            stop_lands_now, TextDelta(text="x", index=0), result("c1"), result("c2"), result("c3"), "BLOCK",
        ])
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid)

        records = _records(fake_workspace_io, sid)
        results = [r for r in records if r["kind"] == SessionMessageKind.TOOL_RESULT]
        assert len(results) == 3, "a Stop mid-batch dropped tool results that had already completed"
        assert [r["kind"] for r in records][-1] == SessionMessageKind.CANCELLED


class TestACancelThatLandsAfterTheModelFinished:
    """An executor that owns the Stop event no longer has dispatch break on a set event, so a Cancel
    that arrives after the model's terminal event used to find the stream already ending: the turn
    completed WAITING with cancel_requested still set ("I cancelled it and nothing happened")."""

    async def _cancel_lands(self, storage, bus, sid: str) -> None:
        sessions = storage.get_storage(WorkspaceSession)
        row = await sessions.get(sid)
        row.cancel_requested = True
        await sessions.update(row)
        await bus.publish(f"session:{sid}:cancel", {})

    async def test_it_ends_the_session_as_cancelled(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id

        async def cancel_lands_after_the_answer() -> None:
            await self._cancel_lands(fake_storage_provider, fake_event_bus, sid)

        executor = _StopAwareExecutor([
            TextDelta(text="the full answer", index=0), cancel_lands_after_the_answer,
            Done(stop_reason="stop", raw_reason="stop"),
        ])
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid)

        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled", (
            f"a Cancel after the model finished left the session {row.status!r}"
        )
        assert SessionMessageKind.CANCELLED in [r["kind"] for r in _records(fake_workspace_io, sid)]

    async def test_it_ends_the_session_even_when_it_lands_just_before_the_completion_transition(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        """The residual window: after dispatch last looked at the row, before it takes the lifecycle
        lock to land the completion. The status must still not land WAITING."""
        sid = seeded_session.id
        cancel_lands = self._cancel_lands

        async def read_status_then_cancel_lands(executor):
            await cancel_lands(fake_storage_provider, fake_event_bus, sid)
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)
        executor = _StopAwareExecutor([
            TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop"),
        ])
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid)

        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"

    async def test_without_a_cancel_the_turn_completes_as_before(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id
        executor = _StopAwareExecutor([
            TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop"),
        ])
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid)

        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status != SessionStatus.ENDED or row.ended_reason != "cancelled"
        assert SessionMessageKind.CANCELLED not in [r["kind"] for r in _records(fake_workspace_io, sid)]


class TestAStopIsNeverLostToTheBus:
    async def test_a_stop_whose_publish_never_arrives_is_delivered_by_the_row_poll(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        monkeypatch.setattr(dispatch, "_INTERRUPT_POLL_S", 0.05)
        executor = _StopAwareExecutor(["BLOCK"])
        sid = seeded_session.id
        turn = asyncio.create_task(_run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid))

        await asyncio.sleep(0.1)
        await _request_stop(fake_storage_provider, fake_event_bus, sid, publish=False)
        outcome = await turn

        assert outcome.success is True
        kinds = [r["kind"] for r in _records(fake_workspace_io, sid)]
        assert SessionMessageKind.CANCELLED in kinds
        assert _poll_catches("missed_while_running") == 1.0, (
            "a Stop requested while the turn ran, whose bus message never came, is the case that "
            "means the bus is dropping Stops"
        )
        assert _poll_catches("queued_before_turn") == 0.0

    async def test_a_stop_delivered_by_the_bus_is_not_counted_as_a_poll_catch(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        executor = _StopAwareExecutor(["BLOCK"])
        sid = seeded_session.id
        turn = asyncio.create_task(_run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid))

        await asyncio.sleep(0.05)
        await _request_stop(fake_storage_provider, fake_event_bus, sid)
        await turn

        assert _poll_catches("missed_while_running") == 0.0 and _poll_catches("queued_before_turn") == 0.0

    async def test_a_stop_recorded_before_the_turn_began_is_honoured_by_the_first_poll(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """Queued Stop: the first poll is immediate, not one interval in (the interval is 2s)."""
        sid = seeded_session.id
        await _request_stop(fake_storage_provider, fake_event_bus, sid, publish=False)
        executor = _StopAwareExecutor(["BLOCK"])

        outcome = await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid, timeout=1.0)

        assert outcome.success is True
        assert SessionMessageKind.CANCELLED in [r["kind"] for r in _records(fake_workspace_io, sid)]
        # Not a bus failure: the flag was simply already there when the turn began. It must not
        # read as "the bus is dropping Stops".
        assert _poll_catches("queued_before_turn") == 1.0 and _poll_catches("missed_while_running") == 0.0

    async def test_a_bus_that_cannot_subscribe_does_not_cost_the_stop(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog,
    ) -> None:
        def broken_subscribe(*_a, **_k):
            raise RuntimeError("bus is down")

        monkeypatch.setattr(dispatch, "_INTERRUPT_POLL_S", 0.05)
        monkeypatch.setattr(fake_event_bus, "subscribe", broken_subscribe)
        executor = _StopAwareExecutor(["BLOCK"])
        sid = seeded_session.id
        with caplog.at_level(logging.WARNING, logger="primer.session.dispatch"):
            turn = asyncio.create_task(_run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid))
            await asyncio.sleep(0.1)
            await _request_stop(fake_storage_provider, fake_event_bus, sid, publish=False)
            outcome = await turn

        assert outcome.success is True
        assert any("bus is down" in r.getMessage() and sid in r.getMessage() for r in caplog.records), (
            "a watcher that cannot subscribe must say so, naming the session"
        )


class _FlakyStorage:
    """Raises on the first read, then returns a row with the Stop flag set."""

    def __init__(self) -> None:
        self.reads = 0

    async def get(self, _sid):
        self.reads += 1
        if self.reads == 1:
            raise RuntimeError("db blip")
        return type("Row", (), {"interrupt_requested": True})()


class TestTheWatcher:
    async def test_the_poll_survives_a_storage_error_and_still_delivers(self, fake_event_bus, monkeypatch, caplog) -> None:
        monkeypatch.setattr(dispatch, "_INTERRUPT_POLL_S", 0.02)
        event = asyncio.Event()
        storage = _FlakyStorage()

        with caplog.at_level(logging.WARNING, logger="primer.session.dispatch"):
            await asyncio.wait_for(
                dispatch._cancel_watcher(fake_event_bus, "s1", event, session_storage=storage), 2.0,
            )

        assert event.is_set() and storage.reads == 2
        assert any("db blip" in r.getMessage() for r in caplog.records)

    async def test_the_bus_path_still_sets_the_event_without_a_storage(self, fake_event_bus) -> None:
        event = asyncio.Event()
        watcher = asyncio.create_task(dispatch._cancel_watcher(fake_event_bus, "s1", event))
        await asyncio.sleep(0.05)

        await fake_event_bus.publish("session:s1:cancel", {})

        await asyncio.wait_for(watcher, 2.0)
        assert event.is_set()

    async def test_cancelling_the_watcher_leaves_no_task_behind(self, fake_event_bus) -> None:
        before = len(asyncio.all_tasks())
        watcher = asyncio.create_task(
            dispatch._cancel_watcher(fake_event_bus, "s1", asyncio.Event(), session_storage=_FlakyStorage()),
        )
        await asyncio.sleep(0.05)

        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await asyncio.sleep(0)

        assert len(asyncio.all_tasks()) == before
