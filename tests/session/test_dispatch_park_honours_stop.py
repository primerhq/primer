"""A Stop (or a Cancel) pending when a turn would PARK ends the turn instead, unless a person is being asked.

Through ``run_one_session_turn`` with the REAL agent loop (the loop-level cases are in ``tests/agent/test_loop_interrupt.py``).
Before, a Stop pressed while the yielding tool was still running (a tool that runs for seconds before it asks to wait) was
dropped: both park arms cleared ``interrupt_requested`` and parked, the console then offered no Stop (an interrupt of a
PARKED row is a 409), and when the timer fired the continuation ran the work the user had tried to stop.

What is pinned here, as the session records it:

* a timer park under a pending Stop does not park: the turn ends WAITING with CANCELLED(operator_interrupt), no park
  columns, no YIELDED record, no ``session.parked`` event, and every call of the round is answered in the transcript
  before the CANCELLED record, so the log is paired;
* a Cancel pending together with the Stop wins: the cancelled exit decides from the row, so the session ENDS cancelled;
* an approval or ``ask_user`` park still parks and the Stop is dropped, as before (a person is being asked);
* a claimed ``tool_wait`` batch ends the same way and creates NO ``ToolCallTask`` row and NO claim (the loop decides
  before the dispatch arm builds anything, so there is nothing to cancel); the notifying call, which already ran, keeps
  its real result.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

import primer.session.dispatch as dispatch
from primer.agent.loop import run_agent_turn
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, Message, TextPart, ToolCallEnd, ToolCallStart, ToolResultPart
from primer.model.model_profile import ModelProfileConfig
from primer.model.external_tool import ExternalToolCall
from primer.model.storage import OffsetPage
from primer.model.tool_call_task import ToolCallTask
from primer.model.workspace_session import SessionMessageKind, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.model_profile import ResolvedModel
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.storage.q import Q
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
    fake_workspace_io,
    seeded_session,
)
from tests.session.test_dispatch_interrupt import _records, _request_stop

STOPPED = "not run: stopped by user"
PARKED_STOP = "interrupted: stopped by user (the call may have run, and its result was not recorded)"
AGENT = Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"), max_tool_turns=10)
MODEL = ResolvedModel(
    profile_id="p", provider_id="prov", model_name="m", context_length=4096, config=ModelProfileConfig(),
)


class _OneRoundLlm:
    """The model asks for ``calls`` (id, tool name) in one round. A second request would be a bug in these tests."""

    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self.calls = calls
        self.requests = 0

    def stream(self, **_kwargs):
        self.requests += 1
        calls = self.calls

        async def gen():
            for index, (call_id, name) in enumerate(calls):
                yield ToolCallStart(id=call_id, name=name, index=index)
                yield ToolCallEnd(id=call_id, arguments={}, index=index)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")

        return gen()


class _Manager:
    """Tools answer at once, except ``parking``: while it RUNS a Stop (and optionally a Cancel) lands, then it asks
    to wait on ``key``. ``notify_tool`` is a notifying call (the runner answers it itself)."""

    def __init__(
        self, *, parking: str | None, key: str, storage, bus, sid: str, also_cancel: bool = False,
        interruptible: bool = False,
    ) -> None:
        self.parking, self.key, self.storage, self.bus, self.sid, self.also_cancel = parking, key, storage, bus, sid, also_cancel
        # False (the default here): the tests below drive the PARK path of a Stop that lands while a call runs, which a call
        # the Stop cancels never reaches. ``TestAStopCancelsTheCallThatWouldPark`` passes True (a call a Stop cancels).
        self.interruptible = interruptible
        self.executed: list[str] = []

    def is_notifying(self, tool_name: str) -> bool:
        return tool_name == "notify_tool"

    def is_interruptible(self, tool_name: str) -> bool:
        return self.interruptible

    async def list_tools(self, *, principal=None):
        return []

    async def deliver_notifying(self, call, *, principal=None):
        return ToolResultPart(id=call.id, output="delivered", error=False)

    async def stop_lands(self) -> None:
        await _request_stop(self.storage, self.bus, self.sid)
        if self.also_cancel:
            sessions = self.storage.get_storage(WorkspaceSession)
            row = await sessions.get(self.sid)
            row.cancel_requested = True
            await sessions.update(row)
        await asyncio.sleep(0.1)                       # the watcher sets the turn's event

    async def execute(self, call, *, principal=None):
        self.executed.append(call.id)
        if call.id == self.parking:
            await self.stop_lands()
            raise YieldToWorker(Yielded(tool_name=call.name, event_key=f"{self.key}{call.id}"), tool_call_id=call.id)
        return ToolResultPart(id=call.id, output="ran", error=False)


class _RealLoopExecutor:
    """The agent loop as the real executor drives it: it owns the Stop event, reports ``was_interrupted`` and lets a
    park exception through."""

    last_done_reason = "tool_use"

    def __init__(self, llm: _OneRoundLlm, manager: _Manager, *, claims: bool = False, barrier=None) -> None:
        self.llm, self.manager, self.barrier = llm, manager, barrier
        self._tool_calls_as_claims_enabled = claims
        self.event = None
        self._resolver = None
        self.was_interrupted = False
        self.stopped_park = None
        self.stopped_calls: list[str] = []

    def bind_interrupt_event(self, event) -> None:
        self.event = event

    def bind_scoped_call_resolver(self, resolver) -> None:
        self._resolver = resolver

    async def invoke(self, messages: list[Any], **_kwargs: Any):
        holder: list[bool] = []
        stopped: list[Any] = []
        stopped_calls: list[str] = []
        self.stopped_park = None
        self.stopped_calls = []
        async for ev in run_agent_turn(
            agent=AGENT, llm=self.llm, llm_model=MODEL, tool_manager=self.manager,
            prompt=[Message(role="user", parts=[TextPart(text="go")])],
            interrupt=self.event, interrupted_out=holder, stopped_park_out=stopped, stopped_calls_out=stopped_calls,
            tool_calls_as_claims_enabled=self._tool_calls_as_claims_enabled,
            resolve_scoped_call=self._resolver, await_dispatch_barrier=self.barrier,
        ):
            yield ev
        self.was_interrupted = bool(holder)
        self.stopped_park = stopped[0] if stopped else None
        self.stopped_calls = list(stopped_calls)


class _RecordingClaims:
    def __init__(self) -> None:
        self.upserted: list[tuple] = []

    async def upsert(self, kind, entity_id: str, **kwargs) -> None:
        self.upserted.append((kind, entity_id))


class _Observed:
    """What a turn published and emitted, for the assertions that are about events and not records."""

    def __init__(self, monkeypatch, bus, session_id: str) -> None:
        self.terminal: list[dict] = []
        self.emitted: list[str] = []
        publish = bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            if key == f"session:{session_id}:terminal":
                self.terminal.append(dict(payload))
            await publish(key, payload)

        bus.publish = spy_publish
        emitted = self.emitted

        class _Recorder:
            async def emit(self, name: str, **_kwargs: Any) -> None:
                emitted.append(name)

        monkeypatch.setattr(dispatch, "_event_recorder", lambda deps: _Recorder())


async def _turn(seeded_session, io, bus, storage, executor, *, claim_engine=None):
    async def build(_session: WorkspaceSession):
        return executor

    deps = SessionDispatchDeps(
        storage_provider=storage, workspace_io=io, event_bus=bus, build_executor=build, claim_engine=claim_engine,
    )
    return await asyncio.wait_for(run_one_session_turn(_make_lease(seeded_session.id), deps), 5.0)


def _kinds(records: list[dict]) -> list[str]:
    return [r["kind"] for r in records]


def _results(records: list[dict]) -> dict[str, dict]:
    return {r["payload"]["call_id"]: r["payload"] for r in records if r["kind"] == SessionMessageKind.TOOL_RESULT}


def _call_ids(records: list[dict]) -> list[str]:
    return [r["payload"]["id"] for r in records if r["kind"] == SessionMessageKind.TOOL_CALL]


class TestAStopPendingWhenATurnWouldParkOnATimer:
    async def test_the_turn_ends_waiting_instead_of_parking_and_the_log_is_paired(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        sid = seeded_session.id
        seen = _Observed(monkeypatch, fake_event_bus, sid)
        llm = _OneRoundLlm([("a", "wait"), ("b", "wait")])
        manager = _Manager(parking="a", key="timer:", storage=fake_storage_provider, bus=fake_event_bus, sid=sid)

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.success and outcome.drop_lease
        assert outcome.park is None, "the session parked on a timer although a Stop was pending"
        assert manager.executed == ["a"] and llm.requests == 1
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.WAITING and row.ended_reason is None
        assert row.interrupt_requested is False and row.parked_status is None
        records = _records(fake_workspace_io, sid)
        assert SessionMessageKind.YIELDED not in _kinds(records), "a park was recorded for a turn that was stopped"
        assert _kinds(records)[-1] == SessionMessageKind.CANCELLED
        assert records[-1]["payload"]["reason"] == "operator_interrupt"
        # Every call of the round is answered, each by a result carrying the id its TOOL_CALL record carries, and all
        # of them before the CANCELLED record.
        calls = _call_ids(records)
        results = _results(records)
        assert sorted(results) == sorted(calls) and len(calls) == 2, f"the round is not paired: {results}"
        assert sorted((p["output"], p["error"]) for p in results.values()) == sorted([(PARKED_STOP, True), (STOPPED, True)])
        assert max(i for i, k in enumerate(_kinds(records)) if k == SessionMessageKind.TOOL_RESULT) < len(records) - 1
        assert seen.terminal == [{"status": "waiting", "ended_reason": None}]
        assert "session.parked" not in seen.emitted

    async def test_the_calls_that_finished_before_the_yielding_one_keep_their_real_results_in_the_transcript(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """A delivered notifying call and a completed call are not reported as 'may have run': they ran, and said so."""
        sid = seeded_session.id
        llm = _OneRoundLlm([("n", "notify_tool"), ("x", "wait"), ("a", "wait"), ("b", "wait")])
        manager = _Manager(parking="a", key="timer:", storage=fake_storage_provider, bus=fake_event_bus, sid=sid)

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.park is None and manager.executed == ["x", "a"]
        records = _records(fake_workspace_io, sid)
        calls = _call_ids(records)
        results = _results(records)
        assert sorted(results) == sorted(calls) and len(calls) == 4, f"the round is not paired: {results}"
        # In the order the round asked for them: n, x, a, b.
        assert [(results[c]["output"], results[c]["error"]) for c in calls] == [
            ("delivered", False), ("ran", False), (PARKED_STOP, True), (STOPPED, True),
        ]

    async def test_without_a_stop_the_timer_park_parks_as_before(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        """The control: the parking call does not call ``stop_lands``, so nothing is pending and the park happens."""
        sid = seeded_session.id
        seen = _Observed(monkeypatch, fake_event_bus, sid)
        llm = _OneRoundLlm([("a", "wait")])
        manager = _Manager(parking="a", key="timer:", storage=fake_storage_provider, bus=fake_event_bus, sid=sid)

        async def no_stop() -> None:
            return None

        manager.stop_lands = no_stop                   # type: ignore[method-assign]
        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.park is not None and outcome.park.parked_event_key == "timer:a"
        records = _records(fake_workspace_io, sid)
        assert SessionMessageKind.YIELDED in _kinds(records)
        assert SessionMessageKind.TOOL_RESULT not in _kinds(records) and SessionMessageKind.CANCELLED not in _kinds(records)
        assert "session.parked" in seen.emitted and seen.terminal == []


class TestAHumanGateStillParks:
    @pytest.mark.parametrize("key", ["tool_approval:", "ask_user:"])
    async def test_the_park_happens_and_the_stop_is_dropped_as_before(
        self, key, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """A person is being asked; whatever they answer later wins over the earlier Stop (the lead's ruling)."""
        sid = seeded_session.id
        llm = _OneRoundLlm([("a", "wait")])
        manager = _Manager(parking="a", key=key, storage=fake_storage_provider, bus=fake_event_bus, sid=sid)

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.park is not None and outcome.park.parked_event_key == f"{key}a"
        records = _records(fake_workspace_io, sid)
        assert SessionMessageKind.YIELDED in _kinds(records) and SessionMessageKind.CANCELLED not in _kinds(records)
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.interrupt_requested is False, "the dropped Stop must not leak into the turn that resumes the park"


class TestCancelBeatsStop:
    async def test_both_pending_at_a_timer_park_end_the_session_as_a_cancel(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        """The loop only sees that the turn's event is set; the cancelled exit it then reaches decides Stop-versus-Cancel
        from the row, and a Cancel always wins: ENDED/cancelled with the operator_cancel reason, not WAITING with
        operator_interrupt. (A hard Cancel that preempts the turn first is the pool's path and unchanged.)"""
        sid = seeded_session.id
        seen = _Observed(monkeypatch, fake_event_bus, sid)
        llm = _OneRoundLlm([("a", "wait")])
        manager = _Manager(
            parking="a", key="timer:", storage=fake_storage_provider, bus=fake_event_bus, sid=sid, also_cancel=True,
        )

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.park is None
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "cancelled")
        records = _records(fake_workspace_io, sid)
        assert records[-1]["kind"] == SessionMessageKind.CANCELLED and records[-1]["payload"]["reason"] == "operator_cancel"
        assert sorted(_results(records)) == sorted(_call_ids(records)), "the call was left unanswered"
        assert seen.terminal == [{"status": "ended", "ended_reason": "cancelled"}]


class _ExternalToolManager(_Manager):
    """What the invoker-supplied tool provider does for ``parking``: write the pending ExternalToolCall row, then yield on
    its ``external_tool:<session>:<call>`` key (``primer/agent/external_tools.py``). The Stop lands in that window."""

    def __init__(self, *args: Any, land_the_stop: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.land_the_stop = land_the_stop

    async def execute(self, call, *, principal=None):
        self.executed.append(call.id)
        if call.id != self.parking:
            return ToolResultPart(id=call.id, output="ran", error=False)
        row = ExternalToolCall(
            session_id=self.sid, tool_call_id=call.id, tool_name="external__lookup", created_at=datetime.now(UTC),
        )
        await self.storage.get_storage(ExternalToolCall).create(row)
        if self.land_the_stop:
            await self.stop_lands()
        raise YieldToWorker(
            Yielded(
                tool_name="external_tool_park", event_key=f"external_tool:{self.sid}:{call.id}",
                resume_metadata={"external_call_row_id": row.id},
            ),
            tool_call_id=call.id,
        )


class TestAStopThatEndsAnExternalToolPark:
    """An invoker-supplied tool is a park that asks no person (the lead's ruling), so a Stop ends it. Its provider writes
    a pending ExternalToolCall row BEFORE it yields, and a turn that ends instead of parking would leave that row
    listed as pending (and answering it a 409). The cancelled exit cancels it, as the cancel and steer routes do."""

    async def _rows(self, storage, sid: str) -> list[ExternalToolCall]:
        page = await storage.get_storage(ExternalToolCall).find(
            Q(ExternalToolCall).where("session_id", sid).build(), OffsetPage(offset=0, length=50),
        )
        return list(page.items)

    async def test_the_pending_row_is_cancelled_when_the_stop_ends_the_park(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id
        llm = _OneRoundLlm([("a", "wait")])
        manager = _ExternalToolManager(
            parking="a", key="", storage=fake_storage_provider, bus=fake_event_bus, sid=sid,
        )

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.park is None
        rows = await self._rows(fake_storage_provider, sid)
        assert [(r.tool_call_id, r.status, r.is_error) for r in rows] == [("a", "cancelled", True)], (
            f"the stopped call's row was left behind: {[(r.tool_call_id, r.status) for r in rows]}"
        )
        assert rows[0].result == {"cancelled": True, "reason": "stopped by user"} and rows[0].resolved_at is not None

    async def test_the_row_is_already_cancelled_when_the_terminal_event_goes_out(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """A listener that waits for the terminal event and then lists the pending calls must not see the stopped call
        as pending (answering it would be a 409)."""
        sid = seeded_session.id
        seen_at_the_terminal: list[list[str]] = []
        publish = fake_event_bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            if key == f"session:{sid}:terminal":
                seen_at_the_terminal.append([r.status for r in await self._rows(fake_storage_provider, sid)])
            await publish(key, payload)

        fake_event_bus.publish = spy_publish
        llm = _OneRoundLlm([("a", "wait")])
        manager = _ExternalToolManager(
            parking="a", key="", storage=fake_storage_provider, bus=fake_event_bus, sid=sid,
        )

        await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert seen_at_the_terminal == [["cancelled"]], f"a pending row was visible at the terminal event: {seen_at_the_terminal}"

    async def test_a_stop_at_a_timer_park_leaves_an_unrelated_pending_row_alone(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """Only an external_tool park has a row to clean up: the cleanup is keyed on the park, not run for every Stop."""
        sid = seeded_session.id
        other = ExternalToolCall(
            session_id=sid, tool_call_id="zzz", tool_name="external__lookup", created_at=datetime.now(UTC),
        )
        await fake_storage_provider.get_storage(ExternalToolCall).create(other)
        llm = _OneRoundLlm([("a", "wait")])
        manager = _Manager(parking="a", key="timer:", storage=fake_storage_provider, bus=fake_event_bus, sid=sid)

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.park is None
        rows = await self._rows(fake_storage_provider, sid)
        assert [(r.tool_call_id, r.status) for r in rows] == [("zzz", "pending")]

    async def test_without_a_stop_the_park_happens_and_the_row_stays_pending(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """The control: nothing pending, so the park happens and the row is the invoker's to answer."""
        sid = seeded_session.id
        llm = _OneRoundLlm([("a", "wait")])
        manager = _ExternalToolManager(
            parking="a", key="", storage=fake_storage_provider, bus=fake_event_bus, sid=sid, land_the_stop=False,
        )

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.park is not None and outcome.park.parked_event_key == f"external_tool:{sid}:a"
        rows = await self._rows(fake_storage_provider, sid)
        assert [(r.tool_call_id, r.status) for r in rows] == [("a", "pending")]


class TestTheExternalCallCleanupCannotFailOrStallTheCancelledExit:
    """``_cancel_the_stopped_external_call`` runs on the cancelled exit, between the CANCELLED record and the terminal
    publish (so a listener never sees a pending row for a turn that has ended). It is best effort: storage that never
    answers may delay the exit by at most ``_BEST_EFFORT_IO_TIMEOUT_S``, and storage that fails must not fail it. In both
    cases the terminal event still goes out exactly once, the session is left stopped (WAITING) and the outcome
    succeeds. The storage is the real in-memory one with ONE method made to hang or raise, so the real
    ``cancel_pending_external`` and the real exit run."""

    async def _stopped_external_turn(self, seeded_session, io, bus, storage, monkeypatch, update):
        # The cleanup's row write is the guarded ``patch_if`` of ``resolve_external_row`` (it was a whole-row
        # ``update``): hand the row it is writing to the test's hook, so the hook still sees the row.
        rows = storage.get_storage(ExternalToolCall)

        async def patch_if(row_id, *args, **kwargs):
            return await update(await rows.get(row_id))

        monkeypatch.setattr(rows, "patch_if", patch_if)
        seen = _Observed(monkeypatch, bus, seeded_session.id)
        llm = _OneRoundLlm([("a", "wait")])
        manager = _ExternalToolManager(parking="a", key="", storage=storage, bus=bus, sid=seeded_session.id)
        outcome = await _turn(seeded_session, io, bus, storage, _RealLoopExecutor(llm, manager))
        return seen, outcome

    async def _assert_the_exit_finished(self, seen, outcome, storage, sid: str) -> None:
        assert outcome.success and outcome.drop_lease and outcome.park is None
        assert seen.terminal == [{"status": "waiting", "ended_reason": None}], f"the terminal event: {seen.terminal}"
        row = await storage.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.WAITING and row.interrupt_requested is False

    async def test_a_cleanup_that_never_returns_delays_the_exit_by_the_bound_and_does_not_stop_it(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog,
    ) -> None:
        monkeypatch.setattr(dispatch, "_BEST_EFFORT_IO_TIMEOUT_S", 0.2)
        caplog.set_level(logging.WARNING, logger="primer.session.dispatch")
        reached: list[str] = []

        async def never(row) -> None:
            reached.append(row.tool_call_id)
            await asyncio.Event().wait()

        seen, outcome = await self._stopped_external_turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, never,
        )

        assert reached == ["a"], "the cleanup never ran: the test is not in its situation"
        await self._assert_the_exit_finished(seen, outcome, fake_storage_provider, seeded_session.id)
        assert any("was not confirmed within" in r.getMessage() for r in caplog.records), "the timeout was not logged"

    async def test_a_cleanup_that_raises_does_not_fail_the_exit(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, caplog,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="primer.session.dispatch")
        reached: list[str] = []

        async def broken(row) -> None:
            reached.append(row.tool_call_id)
            raise RuntimeError("the storage is down")

        seen, outcome = await self._stopped_external_turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch, broken,
        )

        assert reached == ["a"], "the cleanup never ran: the test is not in its situation"
        await self._assert_the_exit_finished(seen, outcome, fake_storage_provider, seeded_session.id)
        assert any(
            "could not cancel the pending external tool call" in r.getMessage() for r in caplog.records
        ), "the failure was not logged"


class TestAStopCancelsTheCallThatWouldPark:
    """Slice B1: the call is still RUNNING when the Stop lands and its tool is interruptible, so the Stop CANCELS it
    before it can ask to park (the tests above model a call that parks within the grace or in the same wake-up). The
    session ends WAITING exactly as for a park a Stop ended, the round is paired, and nothing is left behind: above all
    the pending ``ExternalToolCall`` row an invoker-supplied tool's provider writes BEFORE it yields (a cancel in that
    window never reaches the yield, so the park-keyed cleanup alone would leave it listed as pending)."""

    async def _rows(self, storage, sid: str) -> list[ExternalToolCall]:
        page = await storage.get_storage(ExternalToolCall).find(
            Q(ExternalToolCall).where("session_id", sid).build(), OffsetPage(offset=0, length=50),
        )
        return list(page.items)

    async def test_a_timer_call_is_cancelled_and_the_turn_ends_waiting_with_the_log_paired(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        sid = seeded_session.id
        seen = _Observed(monkeypatch, fake_event_bus, sid)
        llm = _OneRoundLlm([("a", "wait"), ("b", "wait")])
        manager = _Manager(
            parking="a", key="timer:", storage=fake_storage_provider, bus=fake_event_bus, sid=sid, interruptible=True,
        )

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.success and outcome.drop_lease
        assert outcome.park is None, "the session parked on a timer although a Stop cancelled the call"
        assert manager.executed == ["a"] and llm.requests == 1
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.WAITING and row.ended_reason is None
        assert row.interrupt_requested is False and row.parked_status is None
        records = _records(fake_workspace_io, sid)
        assert SessionMessageKind.YIELDED not in _kinds(records)
        assert _kinds(records)[-1] == SessionMessageKind.CANCELLED
        assert records[-1]["payload"]["reason"] == "operator_interrupt"
        calls = _call_ids(records)
        results = _results(records)
        assert sorted(results) == sorted(calls) and len(calls) == 2, f"the round is not paired: {results}"
        assert [(results[c]["output"], results[c]["error"]) for c in calls] == [(PARKED_STOP, True), (STOPPED, True)]
        assert seen.terminal == [{"status": "waiting", "ended_reason": None}]
        assert "session.parked" not in seen.emitted

    async def test_the_pending_row_of_a_cancelled_external_call_is_cancelled(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id
        llm = _OneRoundLlm([("a", "external__lookup")])
        manager = _ExternalToolManager(
            parking="a", key="", storage=fake_storage_provider, bus=fake_event_bus, sid=sid, interruptible=True,
        )

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.park is None
        rows = await self._rows(fake_storage_provider, sid)
        assert [(r.tool_call_id, r.status, r.is_error) for r in rows] == [("a", "cancelled", True)], (
            f"the cancelled call's row was left behind: {[(r.tool_call_id, r.status) for r in rows]}"
        )
        assert rows[0].result == {"cancelled": True, "reason": "stopped by user"} and rows[0].resolved_at is not None

    async def test_the_row_is_already_cancelled_when_the_terminal_event_goes_out(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        sid = seeded_session.id
        seen_at_the_terminal: list[list[str]] = []
        publish = fake_event_bus.publish

        async def spy_publish(key: str, payload: dict) -> None:
            if key == f"session:{sid}:terminal":
                seen_at_the_terminal.append([r.status for r in await self._rows(fake_storage_provider, sid)])
            await publish(key, payload)

        fake_event_bus.publish = spy_publish
        llm = _OneRoundLlm([("a", "external__lookup")])
        manager = _ExternalToolManager(
            parking="a", key="", storage=fake_storage_provider, bus=fake_event_bus, sid=sid, interruptible=True,
        )

        await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert seen_at_the_terminal == [["cancelled"]], f"a pending row was visible at the terminal event: {seen_at_the_terminal}"

    async def test_a_cancelled_call_that_is_not_external_leaves_an_unrelated_pending_row_alone(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider,
    ) -> None:
        """The cleanup is keyed on an EXTERNAL call having been cancelled, not run for every Stop."""
        sid = seeded_session.id
        other = ExternalToolCall(
            session_id=sid, tool_call_id="zzz", tool_name="external__lookup", created_at=datetime.now(UTC),
        )
        await fake_storage_provider.get_storage(ExternalToolCall).create(other)
        llm = _OneRoundLlm([("a", "wait")])
        manager = _Manager(
            parking="a", key="timer:", storage=fake_storage_provider, bus=fake_event_bus, sid=sid, interruptible=True,
        )

        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, _RealLoopExecutor(llm, manager),
        )

        assert outcome.park is None
        rows = await self._rows(fake_storage_provider, sid)
        assert [(r.tool_call_id, r.status) for r in rows] == [("zzz", "pending")]


class TestAStopPendingWhenABatchWouldParkAsClaims:
    async def test_no_row_or_claim_is_created_and_the_notifying_call_keeps_its_result(
        self, seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, monkeypatch,
    ) -> None:
        sid = seeded_session.id
        seen = _Observed(monkeypatch, fake_event_bus, sid)
        claims = _RecordingClaims()
        llm = _OneRoundLlm([("n", "notify_tool"), ("a", "wait")])
        manager = _Manager(parking=None, key="", storage=fake_storage_provider, bus=fake_event_bus, sid=sid)

        async def stop_lands_after_the_loops_check() -> None:
            await manager.stop_lands()

        executor = _RealLoopExecutor(llm, manager, claims=True, barrier=stop_lands_after_the_loops_check)
        outcome = await _turn(
            seeded_session, fake_workspace_io, fake_event_bus, fake_storage_provider, executor, claim_engine=claims,
        )

        assert outcome.success and outcome.park is None, "the batch parked although a Stop was pending"
        assert manager.executed == [], "the claimable call was started in-process"
        tasks = fake_storage_provider.get_storage(ToolCallTask)
        assert await tasks.get(f"{sid}/x:tool:0:1") is None, "a row was created for a batch that was stopped"
        assert await tasks.get(f"{sid}/x:tool:0:2") is None, "a row was created for a batch that was stopped"
        assert claims.upserted == [], f"a claim was created for a stopped batch: {claims.upserted}"
        row = await fake_storage_provider.get_storage(WorkspaceSession).get(sid)
        assert row.status == SessionStatus.WAITING and row.parked_status is None and row.interrupt_requested is False
        records = _records(fake_workspace_io, sid)
        assert SessionMessageKind.YIELDED not in _kinds(records)
        assert records[-1]["kind"] == SessionMessageKind.CANCELLED and records[-1]["payload"]["reason"] == "operator_interrupt"
        calls = _call_ids(records)
        results = _results(records)
        assert sorted(results) == sorted(calls) and len(calls) == 2, f"the batch is not fully answered: {results}"
        assert sorted((p["output"], p["error"]) for p in results.values()) == sorted([("delivered", False), (STOPPED, True)])
        assert SessionMessageKind.CLIENT_ACTION in _kinds(records), "the delivered notifying call's client action was lost"
        assert seen.terminal == [{"status": "waiting", "ended_reason": None}]
        assert "session.parked" not in seen.emitted
