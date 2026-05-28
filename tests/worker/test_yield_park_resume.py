"""End-to-end park + resume test for the M1 yielding-tool prototype.

Exercises the full vertical slice using the InMemoryScheduler:

1. Worker claims a session, agent invokes the yielding ``sleep`` tool.
2. Worker observes ``YieldToWorker`` and calls
   :meth:`Scheduler.park_turn`. WorkspaceSession row carries park fields;
   lease released.
3. External party (event bus, in M2 — simulated here) calls
   :meth:`Scheduler.mark_resumable` with a payload. WorkspaceSession row
   flips to ``resumable``; lease re-armed.
4. Worker re-claims the session, rehydrates park state, calls the
   sleep tool's resume hook with the synthesised payload, produces
   the tool result.

This test pins the M1 contract independent of LLM/executor wiring —
the worker pool's tool engine integration (calling tools with ctx,
catching YieldToWorker, dispatching to resume hooks) is exercised
through more comprehensive M2+ integration tests once the
end-to-end executor path is yield-aware.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from matrix.int.scheduler import CompleteTurnResult
from matrix.model.workspace_session import (
    AgentSessionBinding,
    WorkspaceSession,
    SessionStatus,
)
from matrix.model.yield_ import (
    ToolContext,
    YieldCancelled,
    YieldTimeout,
    YieldToWorker,
    Yielded,
)
from matrix.scheduler.in_memory import InMemoryScheduler, _LeaseState
from matrix.toolset.misc import build_misc_toolset
from matrix.worker.yield_resume_registry import get_resume_hook
from matrix.worker.yield_runtime import (
    ParkedState,
    classify_resume_payload,
    make_cancelled_payload,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
async def scheduler():
    s = InMemoryScheduler()
    await s.initialize()
    yield s
    await s.aclose()


def _make_session(session_id: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id="ws-x",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )


async def _register_worker_and_inject(
    scheduler: InMemoryScheduler,
    session_id: str,
    *,
    worker_id: str | None,
    lease_runnable: bool,
) -> WorkspaceSession:
    """Set up a session + lease pair in the in-memory scheduler.

    Convenience helper for the park/resume tests so each one can
    skip the boilerplate of constructing a lease state object.
    """
    sess = _make_session(session_id)
    await scheduler.register_worker(
        worker_id="wrk-1", host="h", pid=1, capacity=1,
    )
    scheduler._sessions[session_id] = sess
    scheduler._leases[session_id] = _LeaseState(
        worker_id=worker_id,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(minutes=1)
            if worker_id else None
        ),
        runnable=lease_runnable,
        next_attempt_at=datetime.now(timezone.utc),
    )
    return sess


# ===========================================================================
# Park flow
# ===========================================================================


class TestParkFlow:
    @pytest.mark.asyncio
    async def test_park_writes_fields_and_releases_lease(
        self, scheduler: InMemoryScheduler,
    ):
        sess = await _register_worker_and_inject(
            scheduler, "sess-1",
            worker_id="wrk-1", lease_runnable=False,
        )

        provider = build_misc_toolset()
        with pytest.raises(YieldToWorker) as info:
            await provider.call(
                tool_name="sleep",
                arguments={"seconds": 30},
                ctx=ToolContext(
                    tool_call_id="tc-1",
                    session_id="sess-1",
                    workspace_id="ws-x",
                ),
            )

        yielded = info.value.yielded
        parked_at = datetime.now(timezone.utc)
        parked_state = ParkedState(
            yielded=yielded,
            llm_messages=[],
            turn_no=0,
            started_at=parked_at,
        )
        result = await scheduler.park_turn(
            worker_id="wrk-1",
            session_id="sess-1",
            expected_turn_no=0,
            parked_event_key=yielded.event_key,
            parked_until=parked_at + timedelta(seconds=30),
            parked_at=parked_at,
            parked_state=parked_state.to_jsonable(),
        )
        assert result is CompleteTurnResult.SUCCESS

        # WorkspaceSession row has park fields.
        assert sess.parked_status == "parked"
        assert sess.parked_event_key == yielded.event_key
        assert sess.parked_at is not None
        assert sess.parked_until is not None
        assert sess.parked_state is not None
        assert sess.parked_state["yielded"]["tool_name"] == "sleep"

        # Lease released — worker_id None, not runnable.
        lease = scheduler._leases["sess-1"]
        assert lease.worker_id is None
        assert lease.runnable is False
        # turn_no unchanged (park is not a turn-complete).
        assert sess.turn_no == 0

    @pytest.mark.asyncio
    async def test_park_with_stale_turn_no_returns_turn_conflict(
        self, scheduler: InMemoryScheduler,
    ):
        sess = await _register_worker_and_inject(
            scheduler, "sess-2",
            worker_id="wrk-1", lease_runnable=False,
        )
        sess.turn_no = 5

        # Caller's view of turn_no is stale.
        result = await scheduler.park_turn(
            worker_id="wrk-1",
            session_id="sess-2",
            expected_turn_no=2,  # actual is 5
            parked_event_key="timer:tc-1",
            parked_until=datetime.now(timezone.utc),
            parked_at=datetime.now(timezone.utc),
            parked_state={},
        )
        assert result is CompleteTurnResult.TURN_CONFLICT
        # WorkspaceSession NOT parked.
        assert sess.parked_status is None


# ===========================================================================
# Resume flow
# ===========================================================================


class TestResumeFlow:
    @pytest.mark.asyncio
    async def test_mark_resumable_flips_state_and_stamps_payload(
        self, scheduler: InMemoryScheduler,
    ):
        sess = await _register_worker_and_inject(
            scheduler, "sess-3",
            worker_id=None, lease_runnable=False,
        )
        sess.parked_status = "parked"
        sess.parked_event_key = "timer:tc-3"
        sess.parked_until = datetime.now(timezone.utc) + timedelta(seconds=30)
        sess.parked_at = datetime.now(timezone.utc)
        sess.parked_state = {
            "schema_version": 1,
            "yielded": {
                "tool_name": "sleep",
                "event_key": "timer:tc-3",
                "timeout": 30.0,
                "resume_metadata": {"requested_seconds": 30.0},
            },
            "llm_messages": [],
            "turn_no": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "resume_event_payload": None,
        }

        n = await scheduler.mark_resumable(
            "timer:tc-3", resume_event_payload={"some": "event-data"},
        )
        assert n == 1
        assert sess.parked_status == "resumable"
        assert (
            sess.parked_state["resume_event_payload"]
            == {"some": "event-data"}
        )
        # Lease re-armed.
        assert scheduler._leases["sess-3"].runnable is True

    @pytest.mark.asyncio
    async def test_mark_resumable_idempotent_only_first_wins(
        self, scheduler: InMemoryScheduler,
    ):
        sess = await _register_worker_and_inject(
            scheduler, "sess-4",
            worker_id=None, lease_runnable=False,
        )
        sess.parked_status = "parked"
        sess.parked_event_key = "timer:tc-4"

        first = await scheduler.mark_resumable(
            "timer:tc-4", resume_event_payload={"first": True},
        )
        second = await scheduler.mark_resumable(
            "timer:tc-4", resume_event_payload={"second": True},
        )
        assert first == 1
        assert second == 0
        # First payload survived.
        assert sess.parked_state["resume_event_payload"] == {"first": True}

    @pytest.mark.asyncio
    async def test_mark_resumable_unknown_key_returns_zero(
        self, scheduler: InMemoryScheduler,
    ):
        n = await scheduler.mark_resumable(
            "timer:never-existed", resume_event_payload={},
        )
        assert n == 0

    @pytest.mark.asyncio
    async def test_clear_park_nulls_all_parked_columns(
        self, scheduler: InMemoryScheduler,
    ):
        """clear_park is the post-resume sweep: NULLs every parked_*
        column on the session row so subsequent claims see a normal
        non-parked session, and a stale park can't leak into a fresh
        turn's lifecycle. Idempotent on already-cleared rows.
        """
        sess = await _register_worker_and_inject(
            scheduler, "sess-clear",
            worker_id=None, lease_runnable=False,
        )
        # Stamp a parked-resumable shape (mirrors what mark_resumable
        # leaves behind right before the worker dispatches a resume).
        sess.parked_status = "resumable"
        sess.parked_event_key = "timer:tc-clear"
        sess.parked_until = datetime.now(timezone.utc) + timedelta(seconds=30)
        sess.parked_at = datetime.now(timezone.utc)
        sess.parked_state = {
            "schema_version": 1,
            "yielded": {
                "tool_name": "sleep",
                "event_key": "timer:tc-clear",
                "timeout": 30.0,
                "resume_metadata": {},
            },
            "llm_messages": [],
            "turn_no": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "resume_event_payload": {"woken": True},
        }

        await scheduler.clear_park("sess-clear")

        assert sess.parked_status is None
        assert sess.parked_event_key is None
        assert sess.parked_until is None
        assert sess.parked_at is None
        assert sess.parked_state is None

        # Idempotent: second call on an already-clear row is a no-op.
        await scheduler.clear_park("sess-clear")
        assert sess.parked_status is None

    @pytest.mark.asyncio
    async def test_clear_park_on_unknown_session_is_noop(
        self, scheduler: InMemoryScheduler,
    ):
        # No exception, no side effect — defensive against the worker
        # racing storage on a row that's already been deleted.
        await scheduler.clear_park("sess-does-not-exist")


# ===========================================================================
# End-to-end park → mark_resumable → resume hook
# ===========================================================================


def _yield_with_parked_at(
    yielded: Yielded, parked_at: datetime,
) -> Yielded:
    """Mirror what the worker's _handle_yield does — stamp
    parked_at_iso into resume_metadata so the resume hook can
    compute elapsed without a separate read."""
    rm = dict(yielded.resume_metadata)
    rm["parked_at_iso"] = parked_at.isoformat()
    return Yielded(
        tool_name=yielded.tool_name,
        event_key=yielded.event_key,
        timeout=yielded.timeout,
        resume_metadata=rm,
    )


class TestE2EParkAndResume:
    @pytest.mark.asyncio
    async def test_sleep_park_then_timer_event_then_resume(self):
        # 1. Tool yields.
        provider = build_misc_toolset()
        ctx = ToolContext(
            tool_call_id="tc-e2e",
            session_id="sess-e2e",
            workspace_id="ws-x",
        )
        with pytest.raises(YieldToWorker) as info:
            await provider.call(
                tool_name="sleep",
                arguments={"seconds": 5},
                ctx=ctx,
            )
        yielded = info.value.yielded

        # 2. Worker writes park state.
        parked_at = datetime.now(timezone.utc)
        yielded_stamped = _yield_with_parked_at(yielded, parked_at)
        parked_state = ParkedState(
            yielded=yielded_stamped,
            llm_messages=[],
            turn_no=0,
            started_at=parked_at,
        )

        # 3. Round-trip through JSON (what postgres does).
        blob = parked_state.to_jsonable()

        # 4. Timer scheduler (M2) publishes an empty event.
        blob["resume_event_payload"] = {}

        # 5. Worker re-claims, rehydrates, classifies payload.
        rehydrated = ParkedState.from_jsonable(blob)
        payload = classify_resume_payload(
            rehydrated,
            parked_at=parked_at,
            now=parked_at + timedelta(seconds=5),
        )
        assert payload.payload == {}  # real event, empty body
        assert payload.elapsed_seconds == pytest.approx(5.0)

        # 6. Worker dispatches to the tool's resume hook.
        hook = get_resume_hook(rehydrated.yielded.tool_name)
        result = hook(rehydrated.yielded.resume_metadata, payload.payload)
        assert result.is_error is False
        body = json.loads(result.output)
        assert body["requested_seconds"] == 5.0
        # elapsed_seconds is computed in the hook from parked_at_iso.
        assert body["elapsed_seconds"] > 0

    @pytest.mark.asyncio
    async def test_sleep_park_then_cancel_yields_cancelled_result(self):
        provider = build_misc_toolset()
        ctx = ToolContext(
            tool_call_id="tc-cancel",
            session_id="sess-cancel",
            workspace_id="ws-x",
        )
        with pytest.raises(YieldToWorker) as info:
            await provider.call(
                tool_name="sleep",
                arguments={"seconds": 60},
                ctx=ctx,
            )
        yielded = info.value.yielded
        parked_at = datetime.now(timezone.utc)
        yielded_stamped = _yield_with_parked_at(yielded, parked_at)
        parked_state = ParkedState(
            yielded=yielded_stamped,
            llm_messages=[],
            turn_no=0,
            started_at=parked_at,
        )
        blob = parked_state.to_jsonable()
        cancelled_at = parked_at + timedelta(seconds=12)
        blob["resume_event_payload"] = make_cancelled_payload(
            reason="operator changed mind",
            cancelled_at=cancelled_at,
        )
        rehydrated = ParkedState.from_jsonable(blob)
        payload = classify_resume_payload(
            rehydrated, parked_at=parked_at, now=cancelled_at,
        )
        assert isinstance(payload.payload, YieldCancelled)

        hook = get_resume_hook(rehydrated.yielded.tool_name)
        result = hook(rehydrated.yielded.resume_metadata, payload.payload)
        body = json.loads(result.output)
        assert body["cancelled"] is True
        assert body["cancel_reason"] == "operator changed mind"
        # Result is NOT an error — the agent's turn continues.
        assert result.is_error is False

    @pytest.mark.asyncio
    async def test_sleep_park_then_timeout_yields_normal_result(self):
        # Hand the resume hook a YieldTimeout directly — verifies
        # the hook tolerates the synthetic timeout payload type.
        provider = build_misc_toolset()
        ctx = ToolContext(
            tool_call_id="tc-to",
            session_id="sess-to",
            workspace_id="ws-x",
        )
        with pytest.raises(YieldToWorker) as info:
            await provider.call(
                tool_name="sleep", arguments={"seconds": 5}, ctx=ctx,
            )
        yielded = info.value.yielded
        parked_at = datetime.now(timezone.utc)
        rm = dict(yielded.resume_metadata)
        rm["parked_at_iso"] = parked_at.isoformat()

        hook = get_resume_hook("sleep")
        result = hook(rm, YieldTimeout(elapsed_seconds=5.5))
        body = json.loads(result.output)
        assert body["requested_seconds"] == 5.0
        # No "cancelled" key — timeout is the normal case for sleep.
        assert "cancelled" not in body
