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
import gc
import json
import logging
from typing import Any

import pytest

import primer.observability.metrics as metrics
import primer.session.dispatch as dispatch
from primer.agent.interrupt import Interrupted, interruptible
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.model.chat import Done, ExtendedEvent, TextDelta, ToolCallEnd, ToolCallStart, _ExecutorToolResult
from primer.model.envelope import RELAY_EVERY_TURN_KEY
from primer.model.workspace_session import SessionMessageKind, SessionStatus, WorkspaceSession
from primer.observability.turn_log_writer import NoopTurnLogWriter
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


def _build_returning(executor):
    async def build(_session: WorkspaceSession):
        return executor

    return build


def _deps(storage, io, bus, executor) -> SessionDispatchDeps:
    return SessionDispatchDeps(
        storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=_build_returning(executor),
    )


class _RecordingTurnLog(NoopTurnLogWriter):
    """Remembers the class name of every turn-log event appended, and the reason of a cancelled one."""

    def __init__(self) -> None:
        super().__init__()
        self.kinds: list[str] = []
        self.cancel_reasons: list[str | None] = []

    async def append(self, event) -> int:
        self.kinds.append(type(event).__name__)
        if type(event).__name__ == "TurnLogCancelled":
            self.cancel_reasons.append(event.reason)
        return await super().append(event)


class _RecordingDispatcher:
    """The channel dispatcher the final-result relay posts to."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def dispatch_prompt(self, *, envelope, session=None):
        self.texts.append(envelope.prompt)
        return [{"ok": True}]


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
        records = _records(fake_workspace_io, sid)
        assert [r["kind"] for r in records][-1] == SessionMessageKind.CANCELLED, (
            "the transcript must end in CANCELLED, not in a done the user never got"
        )

    async def test_that_window_is_a_whole_cancel_arm_not_just_a_status(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        """A Cancel that lands during the final flush used to change only the status: the turn was still
        counted completed, ``session.replied`` and TurnLogCompleted were emitted, and a thread-mapped
        session's answer was relayed to the channel AFTER the user cancelled."""
        sid = seeded_session.id
        sessions = fake_storage_provider.get_storage(WorkspaceSession)
        row = await sessions.get(sid)
        row.metadata = {
            **(row.metadata or {}),
            SESSION_REPLY_BINDING_KEY: {"channel_id": "ch-1", "anchor": "thr-1", "quiet": False},
            RELAY_EVERY_TURN_KEY: True,
        }
        await sessions.update(row)
        row = await sessions.get(sid)
        ref = dispatch._binding_ref(row)

        emitted: list[str] = []

        class _Recorder:
            async def emit(self, name: str, **_kwargs: Any) -> None:
                emitted.append(name)

        monkeypatch.setattr(dispatch, "_event_recorder", lambda deps: _Recorder())
        turn_log = _RecordingTurnLog()
        dispatcher = _RecordingDispatcher()
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def read_status_then_cancel_lands(executor):
            await cancel_lands(fake_storage_provider, fake_event_bus, sid)
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)
        published: list[tuple[str, dict]] = []
        publish = fake_event_bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            published.append((key, payload))
            await publish(key, payload)

        monkeypatch.setattr(fake_event_bus, "publish", spy_publish)
        executor = _StopAwareExecutor([
            TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop"),
        ])
        deps = SessionDispatchDeps(
            storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
            build_executor=_build_returning(executor), channel_dispatcher=dispatcher,
            turn_log_writer_factory=lambda _io, _sid: turn_log,
        )
        await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 3.0)

        records = _records(fake_workspace_io, sid)
        assert records[-1]["kind"] == SessionMessageKind.CANCELLED
        # The live surfaces are told too: a tick that carries the CANCELLED record's seq (so a reader
        # fetches it) and the terminal event the interactive webhook hold waits on.
        assert (f"session:{sid}:tick", {"seq": records[-1]["seq"]}) in published
        assert (f"session:{sid}:terminal", {"status": "ended", "ended_reason": "cancelled"}) in published
        assert metrics.turns_total.labels(ref, "cancelled")._value.get() == 1.0
        assert metrics.turns_total.labels(ref, "completed")._value.get() == 0.0, "counted completed"
        assert "session.replied" not in emitted, "announced a reply to a session that was cancelled"
        assert "TurnLogCompleted" not in turn_log.kinds and "TurnLogCancelled" in turn_log.kinds
        assert dispatcher.texts == [], "the answer was relayed to the channel after the user cancelled"

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


class TestTheCancelledRecordSaysWhichOneItWas:
    """A Stop and a Cancel both write a CANCELLED record; its ``reason`` is what tells them apart (the console
    labels "operator_interrupt" as "stopped" and anything else as "cancelled"). The cancel arm used a constant
    "operator_interrupt" for BOTH, so a hard Cancel that landed through it read as a Stop."""

    async def _run_with_turn_log(self, storage, io, bus, executor, sid: str) -> _RecordingTurnLog:
        turn_log = _RecordingTurnLog()
        deps = SessionDispatchDeps(
            storage_provider=storage, workspace_io=io, event_bus=bus,
            build_executor=_build_returning(executor), turn_log_writer_factory=lambda _io, _sid: turn_log,
        )
        await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 3.0)
        return turn_log

    def _cancelled(self, io, sid: str) -> dict:
        return next(r for r in _records(io, sid) if r["kind"] == SessionMessageKind.CANCELLED)

    async def test_a_stop_is_recorded_as_operator_interrupt(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id

        async def stop_lands() -> None:
            await _request_stop(fake_storage_provider, fake_event_bus, sid)
            await asyncio.sleep(0.1)

        turn_log = await self._run_with_turn_log(
            fake_storage_provider, fake_workspace_io, fake_event_bus, _StopAwareExecutor([stop_lands, "BLOCK"]), sid,
        )

        assert self._cancelled(fake_workspace_io, sid)["payload"]["reason"] == "operator_interrupt"
        assert turn_log.cancel_reasons == ["operator_interrupt"]

    async def test_a_cancel_that_lands_as_the_stream_ends_is_recorded_as_operator_cancel(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def cancel_lands_after_the_answer() -> None:
            await cancel_lands(fake_storage_provider, fake_event_bus, sid)

        executor = _StopAwareExecutor([
            TextDelta(text="the full answer", index=0), cancel_lands_after_the_answer,
            Done(stop_reason="stop", raw_reason="stop"),
        ])
        turn_log = await self._run_with_turn_log(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid)

        assert self._cancelled(fake_workspace_io, sid)["payload"]["reason"] == "operator_cancel"
        assert turn_log.cancel_reasons == ["operator_cancel"]

    async def test_a_cancel_inside_the_completion_lock_is_recorded_as_operator_cancel(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        sid = seeded_session.id
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def read_status_then_cancel_lands(executor):
            await cancel_lands(fake_storage_provider, fake_event_bus, sid)
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)
        executor = _StopAwareExecutor([
            TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop"),
        ])
        turn_log = await self._run_with_turn_log(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid)

        assert self._cancelled(fake_workspace_io, sid)["payload"]["reason"] == "operator_cancel"
        assert turn_log.cancel_reasons == ["operator_cancel"]


class TestTheCancelArmDecidesInsideTheLock:
    """Stop-versus-Cancel is decided from the row, and the row can change between a read and the lifecycle lock:
    the Cancel route takes that same lock. A decision taken before the lock could land WAITING on a row whose
    ``cancel_requested`` is already set (a Cancel that arrived in between), so it is taken, and the CANCELLED
    record is written, inside the lock, in that order and before the status transition."""

    async def test_a_cancel_that_lands_just_before_the_lock_turns_the_stop_into_a_cancel(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        sid = seeded_session.id
        sessions = fake_storage_provider.get_storage(WorkspaceSession)
        state = {"armed": False, "acquisitions": 0, "injected": False}
        real_lock = dispatch.session_lifecycle_lock

        class _Acquire:
            def __init__(self, inner) -> None:
                self.inner = inner

            async def __aenter__(self):
                if state["armed"]:
                    state["acquisitions"] += 1
                    # The first lock after the stream ends clears turn_status; the SECOND is the cancel arm's.
                    if state["acquisitions"] == 2 and not state["injected"]:
                        state["injected"] = True
                        row = await sessions.get(sid)
                        row.cancel_requested = True
                        await sessions.update(row)
                return await self.inner.__aenter__()

            async def __aexit__(self, *exc):
                return await self.inner.__aexit__(*exc)

        class _Lock:
            def __init__(self) -> None:
                self.real = real_lock()

            def acquire(self, session_id):
                return _Acquire(self.real.acquire(session_id))

        monkeypatch.setattr(dispatch, "session_lifecycle_lock", lambda: _Lock())

        async def stop_lands_then_arm() -> None:
            await _request_stop(fake_storage_provider, fake_event_bus, sid)
            await asyncio.sleep(0.1)                     # the watcher sets the event
            state["armed"] = True

        executor = _StopAwareExecutor([stop_lands_then_arm, "BLOCK"])
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid)

        assert state["injected"], "the test never reached the cancel arm's lock"
        row = await sessions.get(sid)
        assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled", (
            f"a Cancel that landed before the lock left the row {row.status!r} with cancel_requested set"
        )
        cancelled = next(r for r in _records(fake_workspace_io, sid) if r["kind"] == SessionMessageKind.CANCELLED)
        assert cancelled["payload"]["reason"] == "operator_cancel"

    @pytest.mark.parametrize("how", ["stop", "cancel", "cancel-inside-the-completion-lock"])
    async def test_the_cancelled_record_is_written_before_the_row_leaves_the_turn(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, how,
    ) -> None:
        """A reader that sees the new status must find the record that explains it already in the transcript.
        EVERY transition of the turn is checked, not just the last: a premature one would otherwise hide
        behind a correct later one."""
        sid = seeded_session.id
        at_transition: list[list[str]] = []
        real_transition = dispatch._transition_session_status
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def spy_transition(*args, **kwargs):
            at_transition.append([r["kind"] for r in _records(fake_workspace_io, sid)])
            return await real_transition(*args, **kwargs)

        monkeypatch.setattr(dispatch, "_transition_session_status", spy_transition)

        if how == "cancel-inside-the-completion-lock":
            async def read_status_then_cancel_lands(executor):
                await cancel_lands(fake_storage_provider, fake_event_bus, sid)
                return None

            monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)

        async def lands() -> None:
            if how == "stop":
                await _request_stop(fake_storage_provider, fake_event_bus, sid)
            else:
                await cancel_lands(fake_storage_provider, fake_event_bus, sid)
            await asyncio.sleep(0.1)

        if how == "stop":
            script = [lands, "BLOCK"]
        elif how == "cancel":
            script = [TextDelta(text="the full answer", index=0), lands, Done(stop_reason="stop", raw_reason="stop")]
        else:
            script = [TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop")]
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, _StopAwareExecutor(script), sid)

        assert at_transition, "the turn never transitioned the row"
        assert all(SessionMessageKind.CANCELLED in kinds for kinds in at_transition), (
            f"a transition ran before the CANCELLED record was written: {at_transition}"
        )


class TestTheCancelledRecordWriteIsBoundedInTheLock:
    """The CANCELLED record is written while the per-session lifecycle lock is held, and a workspace whose runtime
    connection dropped (a common reason to press Stop) blocks that write until it reconnects, which may be never.
    Every Cancel, Stop, steer, pause, resume and switch of the session queues behind that lock, so an unbounded
    write wedges the whole session. The write is bounded; on a timeout the exit logs, skips the record and still
    decides, transitions and releases."""

    _NEEDLE = f'"kind":"{SessionMessageKind.CANCELLED.value}"'

    def _script(self, how: str, sid: str, storage, bus) -> list[Any]:
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def stop_lands() -> None:
            await _request_stop(storage, bus, sid)
            await asyncio.sleep(0.1)

        async def cancel_lands_mid_turn() -> None:
            await cancel_lands(storage, bus, sid)
            await asyncio.sleep(0.1)

        if how == "stop":
            return [stop_lands, "BLOCK"]
        return [TextDelta(text="the full answer", index=0), cancel_lands_mid_turn,
                Done(stop_reason="stop", raw_reason="stop")]

    @pytest.mark.parametrize("how", ["stop", "cancel"])
    async def test_a_write_that_never_returns_does_not_wedge_the_lock_or_the_turn(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog, how,
    ) -> None:
        sid = seeded_session.id
        ref = dispatch._binding_ref(await fake_storage_provider.get_storage(WorkspaceSession).get(sid))
        monkeypatch.setattr(dispatch, "_CANCELLED_RECORD_WRITE_TIMEOUT_S", 0.2)
        loop = asyncio.get_running_loop()
        hung = asyncio.Event()
        published: list[tuple[str, dict]] = []
        publish = fake_event_bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            published.append((key, payload))
            await publish(key, payload)

        monkeypatch.setattr(fake_event_bus, "publish", spy_publish)
        real_append = fake_workspace_io.append_message_line

        async def append_or_hang(session_id: str, line: bytes) -> None:
            if self._NEEDLE.encode() in line:
                hung.set()
                await asyncio.Event().wait()           # the runtime socket is down and never comes back
            await real_append(session_id, line)

        monkeypatch.setattr(fake_workspace_io, "append_message_line", append_or_hang)
        waited: dict[str, float] = {}

        async def another_operation_on_the_session() -> None:
            """What a steer, a pause or a second Cancel does: take the session's lifecycle lock."""
            await hung.wait()
            started = loop.time()
            async with dispatch.session_lifecycle_lock().acquire(sid):
                waited["s"] = loop.time() - started

        probe = asyncio.ensure_future(another_operation_on_the_session())
        turn_log = _RecordingTurnLog()
        deps = SessionDispatchDeps(
            storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
            build_executor=_build_returning(_StopAwareExecutor(
                self._script(how, sid, fake_storage_provider, fake_event_bus))),
            turn_log_writer_factory=lambda _io, _sid: turn_log,
        )
        with caplog.at_level(logging.WARNING):
            outcome = await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 5.0)
        await asyncio.wait_for(probe, 2.0)

        assert hung.is_set(), "the test never reached the CANCELLED record's write"
        assert waited["s"] < 2.0, f"another operation on the session waited {waited['s']:.1f}s for the lifecycle lock"
        assert outcome.success and outcome.drop_lease
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        if how == "stop":
            assert row.status == SessionStatus.WAITING and row.ended_reason is None
        else:
            assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"
        assert not row.interrupt_requested, "the flag was not cleared"
        assert row.last_seq >= 1 and row.next_unprocessed_seq == row.last_seq + 1, "the exit did not finish its tail"
        reason = "operator_interrupt" if how == "stop" else "operator_cancel"
        assert turn_log.cancel_reasons == [reason], "the turn log entry was lost"
        assert any(key == f"session:{sid}:terminal" for key, _ in published), "the terminal event was lost"
        assert metrics.turns_total.labels(ref, "cancelled")._value.get() == 1.0
        assert all(isinstance(p.get("seq"), int) for key, p in published if key == f"session:{sid}:tick"), (
            "a tick was published for a record that was never written"
        )
        assert any(sid in r.getMessage() and "CANCELLED" in r.getMessage() for r in caplog.records), (
            "the lost record was not logged"
        )
        assert not [r for r in _records(fake_workspace_io, sid) if r["kind"] == SessionMessageKind.CANCELLED]

    @pytest.mark.parametrize("hangs_in", ["append", "flush"])
    async def test_a_hang_in_either_call_of_the_write_is_bounded(self, monkeypatch, hangs_in) -> None:
        """``append`` can do I/O of its own (it flushes records that sat in the buffer past the age limit before
        adding the new one), so the bound covers it as well as ``flush``."""
        monkeypatch.setattr(dispatch, "_CANCELLED_RECORD_WRITE_TIMEOUT_S", 0.1)

        class _HangingWriter:
            async def append(self, record) -> int:
                if hangs_in == "append":
                    await asyncio.Event().wait()
                return 7

            async def flush(self) -> None:
                if hangs_in == "flush":
                    await asyncio.Event().wait()

        seq = await asyncio.wait_for(dispatch._write_cancelled_record(_HangingWriter(), "s1", "operator_cancel"), 2.0)

        assert seq is None

    @pytest.mark.parametrize("record_write", ["lands", "hangs too"])
    async def test_a_slot_mirror_that_never_returns_does_not_wedge_a_cancel(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog,
        record_write,
    ) -> None:
        """A Cancel ENDS the session, and the ENDED transition mirrors that onto the executor's on-disk slot
        (``AgentSession.set_status`` commits ``session.json`` through the same runtime connection). When that
        connection is dead the commit never returns, inside the lifecycle lock: the clear-interrupt, the cursor,
        the terminal publish and the release never run and every later action on the session waits on the lock.
        The mirror is best-effort, so it is bounded and a timeout is logged and skipped."""
        sid = seeded_session.id
        ref = dispatch._binding_ref(await fake_storage_provider.get_storage(WorkspaceSession).get(sid))
        monkeypatch.setattr(dispatch, "_SLOT_MIRROR_TIMEOUT_S", 0.2)
        monkeypatch.setattr(dispatch, "_CANCELLED_RECORD_WRITE_TIMEOUT_S", 0.2)
        loop = asyncio.get_running_loop()
        mirror_hung = asyncio.Event()
        published: list[str] = []
        publish = fake_event_bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            published.append(key)
            await publish(key, payload)

        monkeypatch.setattr(fake_event_bus, "publish", spy_publish)

        class _DeadSlot:
            """``status()`` is an in-memory read; only ``set_status`` commits ``session.json`` over the connection."""

            async def status(self) -> SessionStatus:
                return SessionStatus.RUNNING

            async def set_status(self, status, *, ended_reason=None) -> None:
                mirror_hung.set()
                await asyncio.Event().wait()           # the runtime socket is down and never comes back

        if record_write == "hangs too":
            real_append = fake_workspace_io.append_message_line

            async def append_or_hang(session_id: str, line: bytes) -> None:
                if self._NEEDLE.encode() in line:
                    await asyncio.Event().wait()
                await real_append(session_id, line)

            monkeypatch.setattr(fake_workspace_io, "append_message_line", append_or_hang)
        waited: dict[str, float] = {}

        async def another_operation_on_the_session() -> None:
            await mirror_hung.wait()
            started = loop.time()
            async with dispatch.session_lifecycle_lock().acquire(sid):
                waited["s"] = loop.time() - started

        probe = asyncio.ensure_future(another_operation_on_the_session())
        executor = _StopAwareExecutor(self._script("cancel", sid, fake_storage_provider, fake_event_bus))
        executor.session = _DeadSlot()
        turn_log = _RecordingTurnLog()
        deps = SessionDispatchDeps(
            storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
            build_executor=_build_returning(executor), turn_log_writer_factory=lambda _io, _sid: turn_log,
        )
        with caplog.at_level(logging.WARNING):
            outcome = await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 5.0)
        await asyncio.wait_for(probe, 2.0)

        assert mirror_hung.is_set(), "the test never reached the slot mirror"
        assert waited["s"] < 2.0, f"another operation on the session waited {waited['s']:.1f}s for the lifecycle lock"
        assert outcome.success and outcome.drop_lease
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"
        assert not row.interrupt_requested, "the flag was not cleared"
        assert row.last_seq >= 1 and row.next_unprocessed_seq == row.last_seq + 1, "the exit did not finish its tail"
        assert turn_log.cancel_reasons == ["operator_cancel"], "the turn log entry was lost"
        assert f"session:{sid}:terminal" in published, "the terminal event was lost"
        assert metrics.turns_total.labels(ref, "cancelled")._value.get() == 1.0
        assert any("slot" in r.getMessage() and "not confirmed" in r.getMessage() for r in caplog.records), (
            "the skipped mirror was not logged"
        )

    async def test_a_slow_but_healthy_slot_mirror_still_lands(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        sid = seeded_session.id
        monkeypatch.setattr(dispatch, "_SLOT_MIRROR_TIMEOUT_S", 2.0)
        mirrored: list[tuple[SessionStatus, str | None]] = []

        class _SlowSlot:
            async def status(self) -> SessionStatus:
                return SessionStatus.RUNNING

            async def set_status(self, status, *, ended_reason=None) -> None:
                await asyncio.sleep(0.3)               # well inside the bound
                mirrored.append((status, ended_reason))

        executor = _StopAwareExecutor(self._script("cancel", sid, fake_storage_provider, fake_event_bus))
        executor.session = _SlowSlot()
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid, timeout=5.0)

        assert mirrored == [(SessionStatus.ENDED, "cancelled")], "the bound cut a healthy mirror short"

    async def test_a_write_that_fails_outright_is_not_swallowed(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        """Only a timeout is absorbed. A write that raises (the runtime says it is not connected) is a real
        failure, and it reaches the pool, which releases the lease as failed, exactly as before."""
        sid = seeded_session.id
        real_append = fake_workspace_io.append_message_line

        async def append_or_fail(session_id: str, line: bytes) -> None:
            if self._NEEDLE.encode() in line:
                raise RuntimeError("EPROTOCOL", "Not connected")
            await real_append(session_id, line)

        monkeypatch.setattr(fake_workspace_io, "append_message_line", append_or_fail)
        with pytest.raises(RuntimeError, match="EPROTOCOL"):
            await _run(fake_storage_provider, fake_workspace_io, fake_event_bus,
                       _StopAwareExecutor(self._script("stop", sid, fake_storage_provider, fake_event_bus)), sid)

    @pytest.mark.parametrize("how", ["stop", "cancel"])
    async def test_a_slow_but_healthy_write_still_lands_its_record(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, how,
    ) -> None:
        sid = seeded_session.id
        monkeypatch.setattr(dispatch, "_CANCELLED_RECORD_WRITE_TIMEOUT_S", 2.0)
        real_append = fake_workspace_io.append_message_line

        async def slow_append(session_id: str, line: bytes) -> None:
            if self._NEEDLE.encode() in line:
                await asyncio.sleep(0.3)               # well inside the bound
            await real_append(session_id, line)

        monkeypatch.setattr(fake_workspace_io, "append_message_line", slow_append)
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus,
                   _StopAwareExecutor(self._script(how, sid, fake_storage_provider, fake_event_bus)), sid, timeout=5.0)

        cancelled = [r for r in _records(fake_workspace_io, sid) if r["kind"] == SessionMessageKind.CANCELLED]
        assert [r["payload"]["reason"] for r in cancelled] == (
            ["operator_interrupt"] if how == "stop" else ["operator_cancel"]
        ), "the bound cut a healthy write short"


class TestTheCancelledExitSurvivesAHardPreempt:
    """The pool delivers a Cancel two ways: the cooperative signal this turn watches, and a HARD preempt (a lost-lease
    verdict after the Cancel route dropped the lease, or the user-cancel path) that cancels the whole task wherever it
    is. Once the turn has decided it is ending as a cancelled one, a hard preempt landing INSIDE that exit cut it
    mid-way: the pool's convergence sees a row that is already ENDED and skips it, so the terminal event (the webhook
    hold waits on it), the turn log, the queued-steer drain and the metric were silently lost."""

    @pytest.mark.parametrize("arm", ["a Cancel found after the stream", "a Stop through the cancel arm"])
    async def test_a_task_cancel_in_the_middle_of_the_exit_does_not_cut_it_short(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, arm,
    ) -> None:
        sid = seeded_session.id
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        ref = dispatch._binding_ref(row)
        published: list[str] = []
        publish = fake_event_bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            published.append(key)
            await publish(key, payload)

        monkeypatch.setattr(fake_event_bus, "publish", spy_publish)
        turn_log = _RecordingTurnLog()
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        if arm == "a Stop through the cancel arm":
            async def stop_lands() -> None:
                await _request_stop(fake_storage_provider, fake_event_bus, sid)
                await asyncio.sleep(0.1)

            script = [stop_lands, "BLOCK"]
            expect_status, expect_ended, expect_reason = SessionStatus.WAITING, None, "operator_interrupt"
        else:
            async def read_status_then_cancel_lands(executor):
                await cancel_lands(fake_storage_provider, fake_event_bus, sid)
                return None

            monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)
            script = [TextDelta(text="the full answer", index=0), Done(stop_reason="stop", raw_reason="stop")]
            expect_status, expect_ended, expect_reason = SessionStatus.ENDED, "cancelled", "operator_cancel"
        deps = SessionDispatchDeps(
            storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
            build_executor=_build_returning(_StopAwareExecutor(script)),
            turn_log_writer_factory=lambda _io, _sid: turn_log,
        )
        real_transition = dispatch._transition_session_status
        outer: dict[str, asyncio.Task] = {}
        fired = {"done": False}

        async def transition_then_the_hard_preempt_lands(*args, **kwargs):
            if not fired["done"]:
                fired["done"] = True
                outer["task"].cancel()                # what scope.cancel("preempted") does to the task
                await asyncio.sleep(0.05)             # the exit takes real time while the cancel is pending
            return await real_transition(*args, **kwargs)

        monkeypatch.setattr(dispatch, "_transition_session_status", transition_then_the_hard_preempt_lands)
        outer["task"] = asyncio.ensure_future(run_one_session_turn(_make_lease(sid), deps))

        outcome = await asyncio.wait_for(outer["task"], 5.0)

        # The exit finished, so it hands the pool its own outcome (the pool releases with success=True and
        # on_release writes no terminal ERROR record) and the absorbed cancellation is consumed with it.
        assert outcome.success and outcome.drop_lease, "the exit's own outcome was thrown away"
        assert outer["task"].cancelling() == 0, "the absorbed cancellation was left pending on the task"
        assert fired["done"], "the test never reached the cancelled exit's transition"
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == expect_status and row.ended_reason == expect_ended
        assert f"session:{sid}:terminal" in published, "the terminal event (the webhook hold waits on it) was lost"
        assert turn_log.cancel_reasons == [expect_reason], "the turn log entry was lost"
        assert metrics.turns_total.labels(ref, "cancelled")._value.get() == 1.0, "the cancelled turn was not counted"

    async def test_repeated_cancels_while_the_exit_runs_are_all_absorbed(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog,
    ) -> None:
        """The heartbeat loop re-delivers the preempt every tick for as long as the lease reads lost."""
        sid = seeded_session.id
        published: list[str] = []
        publish = fake_event_bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            published.append(key)
            await publish(key, payload)

        monkeypatch.setattr(fake_event_bus, "publish", spy_publish)
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def read_status_then_cancel_lands(executor):
            await cancel_lands(fake_storage_provider, fake_event_bus, sid)
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)
        outer: dict[str, asyncio.Task] = {}
        real_transition, real_terminal = dispatch._transition_session_status, dispatch._publish_terminal
        cancels = {"n": 0}

        async def cancelling_transition(*args, **kwargs):
            cancels["n"] += 1
            outer["task"].cancel()
            await asyncio.sleep(0)
            return await real_transition(*args, **kwargs)

        async def cancelling_terminal(*args, **kwargs):
            cancels["n"] += 1
            outer["task"].cancel()
            await asyncio.sleep(0)
            return await real_terminal(*args, **kwargs)

        monkeypatch.setattr(dispatch, "_transition_session_status", cancelling_transition)
        monkeypatch.setattr(dispatch, "_publish_terminal", cancelling_terminal)
        executor = _StopAwareExecutor([TextDelta(text="x", index=0), Done(stop_reason="stop", raw_reason="stop")])
        outer["task"] = asyncio.ensure_future(run_one_session_turn(_make_lease(sid), _deps(
            fake_storage_provider, fake_workspace_io, fake_event_bus, executor,
        )))

        with caplog.at_level(logging.INFO):
            outcome = await asyncio.wait_for(outer["task"], 5.0)

        assert cancels["n"] == 2, "the second preempt never landed inside the exit"
        assert f"session:{sid}:terminal" in published, "a repeated preempt cut the exit"
        assert outcome.success and outcome.drop_lease, "the exit's own outcome was thrown away"
        assert outer["task"].cancelling() == 0, "an absorbed cancellation was left pending on the task"
        absorbed_logs = [r for r in caplog.records if "finishing the exit first" in r.getMessage()]
        assert len(absorbed_logs) == 1, f"an absorbed preempt must be logged once, not {len(absorbed_logs)} times"

    async def test_it_consumes_only_the_cancellations_it_absorbed_during_the_exit(self) -> None:
        """A task that was already cancelling when the exit started (an outer scope absorbed an earlier cancel
        without ``uncancel``) keeps that count: only what arrived DURING the exit is consumed."""

        async def the_exit() -> Any:
            await asyncio.sleep(0.05)
            return dispatch.ReleaseOutcome(success=True, drop_lease=True)

        async def body() -> tuple[Any, int, int]:
            me = asyncio.current_task()
            me.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass                                       # swallowed without uncancel(): the count stays at 1
            before = me.cancelling()
            asyncio.get_running_loop().call_later(0.01, me.cancel)    # the preempt, while the exit runs
            outcome = await dispatch._finish_despite_cancel(the_exit())
            return outcome, before, me.cancelling()

        outcome, before, after = await asyncio.wait_for(asyncio.ensure_future(body()), 5.0)

        assert outcome.success
        assert (before, after) == (1, 1), f"cancelling() went {before} -> {after}; the entry count must be kept"

    async def test_two_cancels_in_one_tick_are_both_consumed(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        """``cancelling()`` counts the REQUESTS, but two ``cancel()`` calls before the task runs again deliver ONE
        ``CancelledError``. Consuming one per delivery would leave the task looking cancelled, which a later
        ``asyncio.timeout`` in the pool's release path reads as its own cancellation."""
        sid = seeded_session.id
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def read_status_then_cancel_lands(executor):
            await cancel_lands(fake_storage_provider, fake_event_bus, sid)
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)
        outer: dict[str, asyncio.Task] = {}
        real_transition = dispatch._transition_session_status

        async def double_cancelling_transition(*args, **kwargs):
            outer["task"].cancel()
            outer["task"].cancel()                       # a NOTIFY and the row reconciler both reporting it
            await asyncio.sleep(0.05)
            return await real_transition(*args, **kwargs)

        monkeypatch.setattr(dispatch, "_transition_session_status", double_cancelling_transition)
        executor = _StopAwareExecutor([TextDelta(text="x", index=0), Done(stop_reason="stop", raw_reason="stop")])
        outer["task"] = asyncio.ensure_future(run_one_session_turn(_make_lease(sid), _deps(
            fake_storage_provider, fake_workspace_io, fake_event_bus, executor,
        )))

        outcome = await asyncio.wait_for(outer["task"], 5.0)

        assert outcome.success and outcome.drop_lease
        assert outer["task"].cancelling() == 0, "a cancellation request was left pending on the task"

    async def test_an_exit_that_hangs_is_abandoned_after_the_grace_so_a_drain_can_still_abort_it(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        sid = seeded_session.id
        monkeypatch.setattr(dispatch, "_TERMINAL_EXIT_GRACE_S", 0.2, raising=False)
        hung = asyncio.Event()
        cancelled_inside = asyncio.Event()

        async def hanging_transition(*args, **kwargs):
            hung.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_inside.set()
                raise

        monkeypatch.setattr(dispatch, "_transition_session_status", hanging_transition)
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def read_status_then_cancel_lands(executor):
            await cancel_lands(fake_storage_provider, fake_event_bus, sid)
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)
        executor = _StopAwareExecutor([TextDelta(text="x", index=0), Done(stop_reason="stop", raw_reason="stop")])
        task = asyncio.ensure_future(run_one_session_turn(_make_lease(sid), _deps(
            fake_storage_provider, fake_workspace_io, fake_event_bus, executor,
        )))

        await asyncio.wait_for(hung.wait(), 5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5.0)

        assert cancelled_inside.is_set(), "the hung exit was left running after the grace instead of being abandoned"

    async def test_an_exit_that_fails_after_the_preempt_still_raises_the_cancellation(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog,
    ) -> None:
        """The pool's preempt convergence runs only on a CancelledError. An exit that died (storage down) must not
        turn the absorbed cancel into the exit's own error: the pool would then skip the convergence and leave the
        session RUNNING with no lease."""
        sid = seeded_session.id
        outer: dict[str, asyncio.Task] = {}

        async def failing_transition(*args, **kwargs):
            outer["task"].cancel()
            await asyncio.sleep(0.05)
            raise RuntimeError("storage is down")

        monkeypatch.setattr(dispatch, "_transition_session_status", failing_transition)
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def read_status_then_cancel_lands(executor):
            await cancel_lands(fake_storage_provider, fake_event_bus, sid)
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)
        executor = _StopAwareExecutor([TextDelta(text="x", index=0), Done(stop_reason="stop", raw_reason="stop")])
        outer["task"] = asyncio.ensure_future(run_one_session_turn(_make_lease(sid), _deps(
            fake_storage_provider, fake_workspace_io, fake_event_bus, executor,
        )))

        with caplog.at_level(logging.WARNING):
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(outer["task"], 5.0)

        assert isinstance(raised.value.__cause__, RuntimeError), "the exit's own error was not chained to the cancel"
        assert any("storage is down" in str(r.exc_info[1]) for r in caplog.records if r.exc_info), (
            "the exit's error was swallowed without a log"
        )

    async def test_an_abandoned_exit_that_dies_is_retrieved_and_logged(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog,
    ) -> None:
        """Past the grace the exit is cancelled and nobody awaits it. Whatever it dies of must still be retrieved,
        or asyncio reports 'Task exception was never retrieved' when the task is collected."""
        sid = seeded_session.id
        monkeypatch.setattr(dispatch, "_TERMINAL_EXIT_GRACE_S", 0.2, raising=False)
        hung = asyncio.Event()

        async def hanging_then_failing_transition(*args, **kwargs):
            hung.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failed while being abandoned") from None

        monkeypatch.setattr(dispatch, "_transition_session_status", hanging_then_failing_transition)
        cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

        async def read_status_then_cancel_lands(executor):
            await cancel_lands(fake_storage_provider, fake_event_bus, sid)
            return None

        monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_cancel_lands)
        loop = asyncio.get_running_loop()
        reported: list[str] = []
        loop.set_exception_handler(lambda _loop, context: reported.append(str(context.get("message"))))
        try:
            executor = _StopAwareExecutor([TextDelta(text="x", index=0), Done(stop_reason="stop", raw_reason="stop")])
            task = asyncio.ensure_future(run_one_session_turn(_make_lease(sid), _deps(
                fake_storage_provider, fake_workspace_io, fake_event_bus, executor,
            )))
            await asyncio.wait_for(hung.wait(), 5.0)
            task.cancel()
            with caplog.at_level(logging.WARNING):
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5.0)
                await asyncio.sleep(0.1)                  # the abandoned exit unwinds and dies
            del task
            gc.collect()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(None)

        assert not [m for m in reported if "never retrieved" in m], f"an abandoned exit's error was left: {reported}"
        assert any("cleanup failed while being abandoned" in str(r.exc_info[1]) for r in caplog.records if r.exc_info), (
            "the abandoned exit's error was not logged"
        )


def _stop_lands_script(storage, bus, sid: str):
    """A script step: the Stop lands (flag and bus key) and the watcher has a moment to set the event."""

    async def stop_lands() -> None:
        await _request_stop(storage, bus, sid)
        await asyncio.sleep(0.1)

    return stop_lands


class _DyingTurnLog(NoopTurnLogWriter):
    """A turn log whose every write and close hangs once ``dead`` is set (the runtime socket dropped)."""

    def __init__(self, dead: asyncio.Event) -> None:
        super().__init__()
        self.dead = dead
        self.kinds: list[str] = []

    async def append(self, event) -> int:
        self.kinds.append(type(event).__name__)
        if self.dead.is_set():
            await asyncio.Event().wait()
        return await super().append(event)

    async def aclose(self) -> None:
        self.kinds.append("aclose")
        if self.dead.is_set():
            await asyncio.Event().wait()
        await super().aclose()


class TestTheCancelledExitsOtherWorkspaceIoIsBounded:
    """The CANCELLED record and the slot mirror are bounded inside the lock. The cancelled exit also does best-effort
    workspace I/O OUTSIDE it: the output the model had streamed when the Stop landed (its append can run the writer's
    age flush), the turn-log entry and the turn log's close. On a dead connection the lock is free, but a write that
    never returns still holds the terminal publish and the lease release behind it. EVERY such write hangs here, from
    the moment the Stop lands."""

    def _script(self, how: str, sid: str, storage, bus, dead: asyncio.Event) -> list[Any]:
        async def io_dies_then_stop() -> None:
            await asyncio.sleep(0.2)             # the tool_call record has sat in the writer's buffer past its age limit
            dead.set()
            await _request_stop(storage, bus, sid)
            await asyncio.sleep(0.1)

        streamed = [
            TextDelta(text="first", index=0),
            ToolCallStart(id="t1", name="x", index=0), ToolCallEnd(id="t1", arguments={}, index=0),
            TextDelta(text="the part streamed when the stop landed", index=1),
        ]
        if how == "stop":
            return [*streamed, io_dies_then_stop, "BLOCK"]
        return [*streamed, Done(stop_reason="stop", raw_reason="stop")]

    @pytest.mark.parametrize("how", ["stop", "cancel"])
    async def test_every_best_effort_write_hanging_does_not_hold_the_terminal_publish_or_the_release(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog, how,
    ) -> None:
        sid = seeded_session.id
        ref = dispatch._binding_ref(await fake_storage_provider.get_storage(WorkspaceSession).get(sid))
        monkeypatch.setattr(dispatch, "_BEST_EFFORT_IO_TIMEOUT_S", 0.2)
        monkeypatch.setattr(dispatch, "_CANCELLED_RECORD_WRITE_TIMEOUT_S", 0.2)
        dead = asyncio.Event()
        published: list[str] = []
        publish = fake_event_bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            published.append(key)
            await publish(key, payload)

        monkeypatch.setattr(fake_event_bus, "publish", spy_publish)
        real_append = fake_workspace_io.append_message_line

        async def append_or_hang(session_id: str, line: bytes) -> None:
            if dead.is_set():
                await asyncio.Event().wait()
            await real_append(session_id, line)

        monkeypatch.setattr(fake_workspace_io, "append_message_line", append_or_hang)
        if how == "cancel":
            cancel_lands = TestACancelThatLandsAfterTheModelFinished()._cancel_lands

            async def read_status_then_the_workspace_dies_and_a_cancel_lands(executor):
                # The stream is over: only the cancelled exit is left, and every write in it now hangs.
                dead.set()
                await cancel_lands(fake_storage_provider, fake_event_bus, sid)
                return None

            monkeypatch.setattr(dispatch, "_read_agent_session_status", read_status_then_the_workspace_dies_and_a_cancel_lands)
        turn_log = _DyingTurnLog(dead)
        deps = SessionDispatchDeps(
            storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
            build_executor=_build_returning(_StopAwareExecutor(
                self._script(how, sid, fake_storage_provider, fake_event_bus, dead))),
            turn_log_writer_factory=lambda _io, _sid: turn_log,
        )
        with caplog.at_level(logging.WARNING):
            outcome = await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 10.0)

        assert dead.is_set(), "the test never reached the point where the workspace dies"
        assert outcome.success and outcome.drop_lease, "the lease was not released"
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        if how == "stop":
            assert row.status == SessionStatus.WAITING and row.ended_reason is None
        else:
            assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"
        assert f"session:{sid}:terminal" in published, "the terminal event was held behind a hung write"
        assert metrics.turns_total.labels(ref, "cancelled")._value.get() == 1.0
        assert "TurnLogCancelled" in turn_log.kinds and turn_log.kinds[-1] == "aclose", (
            "the exit never reached the turn log's entry and its close"
        )
        text = " | ".join(r.getMessage() for r in caplog.records)
        assert "turn log entry" in text and "closing the turn log" in text, f"a skipped write was not logged: {text}"
        if how == "stop":
            assert "streamed before the stop" in text, "the skipped partial-output flush was not logged"

    async def test_a_slow_but_healthy_turn_log_still_gets_its_entry_and_its_close(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        sid = seeded_session.id
        monkeypatch.setattr(dispatch, "_BEST_EFFORT_IO_TIMEOUT_S", 2.0)
        landed: list[str] = []

        class _SlowTurnLog(NoopTurnLogWriter):
            async def append(self, event) -> int:
                await asyncio.sleep(0.3)               # well inside the bound
                landed.append(type(event).__name__)
                return await super().append(event)

            async def aclose(self) -> None:
                await asyncio.sleep(0.3)
                landed.append("aclose")
                await super().aclose()

        script = [TextDelta(text="x", index=0), _stop_lands_script(fake_storage_provider, fake_event_bus, sid), "BLOCK"]
        deps = SessionDispatchDeps(
            storage_provider=fake_storage_provider, workspace_io=fake_workspace_io, event_bus=fake_event_bus,
            build_executor=_build_returning(_StopAwareExecutor(script)),
            turn_log_writer_factory=lambda _io, _sid: _SlowTurnLog(),
        )
        await asyncio.wait_for(run_one_session_turn(_make_lease(sid), deps), 10.0)

        assert "TurnLogCancelled" in landed and landed[-1] == "aclose", f"the bound cut a healthy write short: {landed}"

    async def test_a_failure_that_is_not_a_timeout_is_not_swallowed(self) -> None:
        async def boom() -> None:
            raise RuntimeError("the turn log is broken")

        with pytest.raises(RuntimeError, match="broken"):
            await dispatch._best_effort_io("closing the turn log", "s1", boom())

    @pytest.mark.parametrize("hangs_in", ["get_workspace", "get_session"])
    async def test_the_build_failure_fallback_that_loads_the_slot_is_bounded_too(
        self, monkeypatch, caplog, hangs_in,
    ) -> None:
        """With no executor (the build failed) the slot is re-resolved through the workspace registry, which also
        goes over the runtime connection. It is part of the mirror, so it gets the mirror's bound."""
        monkeypatch.setattr(dispatch, "_SLOT_MIRROR_TIMEOUT_S", 0.2)

        class _Workspace:
            async def get_session(self, session_id: str):
                await asyncio.Event().wait()

        class _Registry:
            async def get_workspace(self, workspace_id: str):
                if hangs_in == "get_workspace":
                    await asyncio.Event().wait()
                return _Workspace()

        session = type("S", (), {"id": "s1", "workspace_id": "w1"})()
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(
                dispatch._sync_agent_session_ended(None, "failed", session=session, workspace_registry=_Registry()),
                3.0,
            )

        assert any("failed to load the on-disk AgentSession slot" in r.getMessage() for r in caplog.records)


class TestAStopThatLandsBeforeTheBatchThroughTheWholeTurn:
    """The loop-level tests prove the refusal; this proves what the SESSION records and ends as when the loop is the
    real one: the tool never runs, the call is answered in the transcript, and the session rests WAITING."""

    async def test_a_stop_as_the_model_finishes_runs_no_tool_and_keeps_the_log_paired(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        from primer.agent.loop import run_agent_turn
        from primer.model.agent import Agent, AgentModel
        from primer.model.chat import Message, TextPart, ToolCallEnd, ToolCallStart, ToolResultPart
        from primer.model.model_profile import ModelProfileConfig
        from primer.model_profile import ResolvedModel

        sid = seeded_session.id
        executed: list[str] = []

        class _Manager:
            def is_notifying(self, tool_name: str) -> bool:
                return False

            async def list_tools(self, *, principal=None):
                return []

            async def execute(self, call, *, principal=None):
                executed.append(call.id)
                return ToolResultPart(id=call.id, output="ran", error=False)

        class _Llm:
            def stream(self, **_kwargs):
                async def gen():
                    yield ToolCallStart(id="tc1", name="loop_tool", index=0)
                    yield ToolCallEnd(id="tc1", arguments={}, index=0)
                    yield Done(stop_reason="tool_use", raw_reason="tool_use")
                    # The model has finished; the provider just has not closed the stream. The Stop lands in
                    # that drain, so the round is complete (Done and its tool call are in) when it is seen.
                    await _request_stop(fake_storage_provider, fake_event_bus, sid)
                    await asyncio.sleep(30)

                return gen()

        class _RealLoopExecutor:
            last_done_reason = "tool_use"

            def __init__(self) -> None:
                self.event = None
                self.was_interrupted = False

            def bind_interrupt_event(self, event) -> None:
                self.event = event

            async def invoke(self, messages, **_kwargs):
                holder: list[bool] = []
                async for ev in run_agent_turn(
                    agent=Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=10),
                    llm=_Llm(), llm_model=ResolvedModel(
                        profile_id="p", provider_id="prov", model_name="m", context_length=4096,
                        config=ModelProfileConfig(),
                    ),
                    tool_manager=_Manager(), prompt=[Message(role="user", parts=[TextPart(text="go")])],
                    interrupt=self.event, interrupted_out=holder,
                ):
                    yield ev
                self.was_interrupted = bool(holder)

        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, _RealLoopExecutor(), sid)

        assert executed == [], "a Stop that landed before the batch let the tool run"
        records = _records(fake_workspace_io, sid)
        kinds = [r["kind"] for r in records]
        results = [r for r in records if r["kind"] == SessionMessageKind.TOOL_RESULT]
        calls = [r for r in records if r["kind"] == SessionMessageKind.TOOL_CALL]
        # Dispatch rewrites the provider's call id to a scoped one: pair by what the transcript itself records.
        assert len(calls) == 1 and [r["payload"].get("call_id") for r in results] == [calls[0]["payload"].get("id")], (
            "the call is not answered in the transcript"
        )
        assert results[0]["payload"]["output"] == "not run: stopped by user" and results[0]["payload"]["error"] is True
        assert kinds.index(SessionMessageKind.TOOL_RESULT) < kinds.index(SessionMessageKind.CANCELLED)
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.WAITING and row.ended_reason is None


class TestAStopFollowedByAHumanSteer:
    """A steer that lands while a turn runs flips ``turn_status`` to claimable, and ``wake_session`` then
    clears ``interrupt_requested`` on the row. The cancel arm used to decide Stop-vs-End from that flag,
    so a Stop pressed during a long batch followed by a steer landed ENDED/cancelled instead of WAITING.
    A Cancel always sets ``cancel_requested`` BEFORE it publishes; that is what separates the two."""

    async def _steer_clears_the_flag(self, storage, sid: str) -> None:
        sessions = storage.get_storage(WorkspaceSession)
        row = await sessions.get(sid)
        row.interrupt_requested = False
        row.turn_status = "claimable"
        await sessions.update(row)

    async def test_the_steer_clearing_the_flag_does_not_turn_the_stop_into_an_end(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id

        async def stop_then_steer() -> None:
            await _request_stop(fake_storage_provider, fake_event_bus, sid)
            await asyncio.sleep(0.1)                     # the watcher sets the event
            await self._steer_clears_the_flag(fake_storage_provider, sid)

        executor = _StopAwareExecutor([stop_then_steer, "BLOCK"])
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid)

        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.WAITING and row.ended_reason is None, (
            f"a Stop followed by a steer ended the session ({row.status!r}/{row.ended_reason!r})"
        )
        cancelled = next(r for r in _records(fake_workspace_io, sid) if r["kind"] == SessionMessageKind.CANCELLED)
        assert cancelled["payload"]["reason"] == "operator_interrupt"

    async def test_a_real_cancel_is_still_an_end_even_with_the_stop_flag_cleared(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id
        sessions = fake_storage_provider.get_storage(WorkspaceSession)

        async def stop_then_cancel() -> None:
            await _request_stop(fake_storage_provider, fake_event_bus, sid)
            await self._steer_clears_the_flag(fake_storage_provider, sid)
            row = await sessions.get(sid)
            row.cancel_requested = True
            await sessions.update(row)
            await fake_event_bus.publish(f"session:{sid}:cancel", {})

        # The Cancel lands as the stream ends (the shape TestACancelThatLandsAfterTheModelFinished
        # uses): a hard Cancel that lands while the executor is blocked preempts by cancelling the turn.
        executor = _StopAwareExecutor([
            TextDelta(text="the full answer", index=0), stop_then_cancel,
            Done(stop_reason="stop", raw_reason="stop"),
        ])
        await _run(fake_storage_provider, fake_workspace_io, fake_event_bus, executor, sid)

        row = await sessions.get(sid)
        assert row.status == SessionStatus.ENDED and row.ended_reason == "cancelled"


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
