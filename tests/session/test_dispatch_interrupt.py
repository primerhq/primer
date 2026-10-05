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
from primer.channel.reply_binding import SESSION_REPLY_BINDING_KEY
from primer.model.chat import Done, ExtendedEvent, TextDelta, _ExecutorToolResult
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
