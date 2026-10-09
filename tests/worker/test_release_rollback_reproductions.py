"""Reproductions of two release-rollback bugs (tickets 01a10b54-3212 and 01a10b54-425b). 425b is FIXED; 3212 is still a strict xfail.

A claim release runs in ONE transaction (``SessionClaimAdapter.on_release``); when it is abandoned at the pool's bound (or
raises) it rolls back, the lease stays claimed until it expires, and the next claim reaches the pool again. The existing
pin for a COMPLETED turn is ``test_completed_turn_reclaim.py`` (a marker written before the release). Two other release
shapes have no such guard, and each is reproduced here exactly as it happens today:

* a PARK release (3212): the park's records are durable but the park COLUMNS exist only inside the release transaction, so
  the re-claim runs a fresh turn over a log that ends in a dangling ``yielded`` record: the model is called again at the
  same ``turn_no`` and the question the operator was asked is never waited for. Flag ON, a ``tool_wait`` park is worse:
  the park arm already advanced ``last_seq`` before returning its outcome, so the re-run computes different record seqs
  and the strict ``ToolCallTask`` create raises ``TurnInvariantError``: the session ends FAILED.
* a RESUME release (425b, FIXED): the resume handlers do their effects (inject the reply, append records, run an approved
  tool) and clear the park in the release, so a rollback left ``parked_status="resumable"`` and the re-claim took the resume
  branch again: the handler ran twice. The continue path now records the park it applied (``resumed_park_at``) and the
  pool's resume branch skips the handler for a row whose marker names its park; the behaviour test below is a plain test
  and ``test_resume_applied_marker.py`` pins the rest of the contract.

Each reproduction has two tests. The SCENARIO test is a plain test: it pins that the release really was abandoned and what
the world looks like before the re-claim, so a broken harness cannot hide behind an expected failure. The BEHAVIOUR test
asserts what SHOULD happen and, while a bug is open, is a strict xfail restricted to ``AssertionError``: it fails today (that is
the bug), and the fix that makes it pass must delete the marker (strict means an unexpected pass is an error). The 3212 tests
are the red-first tests of its later fix (the ``pending_park`` stash applied in the release). Design direction accepted by the
lead on 2026-10-06.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.chat import Done, Message, TextDelta, TextPart, ToolCallPart, ToolResultPart
from primer.model.tool_call_task import ToolCallTask
from primer.model.workspace_session import AgentSessionBinding, SessionMessageKind, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded, YieldToWorker
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn

from tests.conftest import _FakeStorageProvider
from tests.session.test_tool_wait_seam_e2e import (
    _FakeEventBus,
    _FakeWorkspaceIO,
    _RecordingClaimEngine,
    _ToolWaitExecutor,
    _batch_park,
    _now,
)
from tests.worker.test_completed_turn_reclaim import KEY, SID, _CountingExecutor, _World, world  # noqa: F401
from tests.worker.test_engine_session_resume import (
    _build_engine,
    _build_pool,
    _make_resumable_session,
    _NoopPersist,
    _RecordingExecutor,
    _async_return,
)

_BUG_3212 = "01a10b54-3212: a park release that rolls back leaves no park, and the re-claim runs a fresh turn"
_BUG_3212_FLAG_ON = (
    "01a10b54-3212 (flag on): the park arm advanced last_seq before the release rolled back, so the re-run's record_seq "
    "differs and the strict ToolCallTask create raises TurnInvariantError: the session ends failed"
)


# ---------------------------------------------------------------------------
# 3212: a PARK release abandoned before its commit (flag off, an ask_user park through the real pool)
# ---------------------------------------------------------------------------


class _AskThenAnswer(_CountingExecutor):
    """The first model call parks on ``ask_user``; any later call just answers (so a re-run is visible as a 2nd call)."""

    async def invoke(self, messages, **kwargs):
        self._world.llm_calls.append("park-or-answer")
        self._world.call_turn_nos.append(self._session.turn_no)
        yield TextDelta(text="let me ask", index=0)
        if len(self._world.llm_calls) == 1:
            raise YieldToWorker(
                Yielded(
                    tool_name="ask_user", event_key=f"ask_user:{SID}:tc-1", timeout=600.0,
                    resume_metadata={"prompt": "colour?"},
                ),
                tool_call_id="tc-1",
                llm_messages=[Message(role="assistant", parts=[TextPart(text="let me ask")])],
            )
        yield Done(stop_reason="stop", raw_reason="stop")


async def _park_release_abandoned_then_reclaimed(world: _World) -> dict:
    await world.create_session()
    await world.steer("first")
    pool_a = world.pool("wrk-a")

    async def build_a(session):
        world.builds += 1
        return _AskThenAnswer(world, session)

    pool_a._build_session_executor = build_a  # type: ignore[method-assign]
    world.abandon_next_releases = 1  # the PARK release never reaches the real release: rolled back
    assert await world.claim_and_run(pool_a) == 1
    after_park_turn = await world.row()
    seen = {
        "abandoned": world.abandoned,
        "outcome_parks": [o.park is not None for o in world.outcomes],
        "row_after_park_turn": after_park_turn,
        "lease_holder": world.engine._leases[KEY].claimed_by,
        "kinds_after_park_turn": [r["kind"] for r in world.ws.records()],
    }

    world.expire_lease()
    pool_b = world.pool("wrk-b")

    async def build_b(session):
        world.builds += 1
        return _AskThenAnswer(world, session)

    pool_b._build_session_executor = build_b  # type: ignore[method-assign]
    assert await world.claim_and_run(pool_b) == 1
    seen["row_after_reclaim"] = await world.row()
    seen["kinds_after_reclaim"] = [r["kind"] for r in world.ws.records()]
    seen["model_calls"] = len(world.llm_calls)
    seen["call_turn_nos"] = list(world.call_turn_nos)
    return seen


@pytest.mark.asyncio
async def test_scenario_a_park_release_that_rolled_back_left_a_durable_yielded_record_and_no_park(world):
    seen = await _park_release_abandoned_then_reclaimed(world)

    assert seen["abandoned"] == 1 and seen["outcome_parks"] == [True], "the park release was not the abandoned one"
    row = seen["row_after_park_turn"]
    assert (row.status, row.turn_no, row.parked_status) == (SessionStatus.RUNNING, 0, None), "the park columns were written"
    assert seen["lease_holder"] == "wrk-a", "the old worker's lease should still be claimed"
    assert seen["kinds_after_park_turn"][-1] == SessionMessageKind.YIELDED.value, "the park record is durable before the release"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_BUG_3212)
@pytest.mark.asyncio
async def test_a_re_claim_after_a_rolled_back_park_release_applies_the_park_without_a_second_model_call(world):
    seen = await _park_release_abandoned_then_reclaimed(world)

    assert seen["model_calls"] == 1, (
        f"the re-claim ran a fresh turn: {seen['model_calls']} model calls at turn_nos {seen['call_turn_nos']}; the "
        "ask_user question the operator was asked is never waited for"
    )
    assert seen["row_after_reclaim"].parked_status is not None, "the session did not end parked on its question"
    assert seen["kinds_after_reclaim"].count(SessionMessageKind.YIELDED.value) == 1, "the park was recorded twice"


# ---------------------------------------------------------------------------
# 3212, flag ON: a tool_wait park (a claims batch) whose release is abandoned
# ---------------------------------------------------------------------------


async def _tool_wait_park_release_abandoned_then_reclaimed() -> dict:
    storage = _FakeStorageProvider()
    sessions = storage.get_storage(WorkspaceSession)
    tasks = storage.get_storage(ToolCallTask)
    session = WorkspaceSession(
        id="s-park-rollback", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING, created_at=_now(), turn_status="running",
    )
    await sessions.create(session)
    park = _batch_park(ToolResultPart(id="call_c", output="notified ok", error=False))
    io = _FakeWorkspaceIO()
    builds: list[int] = []

    async def build_executor(_session: WorkspaceSession):
        builds.append(1)
        return _ToolWaitExecutor(park)  # carries _tool_calls_as_claims_enabled = True: the flag-on shape

    deps = SessionDispatchDeps(
        storage_provider=storage, workspace_io=io, event_bus=_FakeEventBus(),
        build_executor=build_executor, claim_engine=_RecordingClaimEngine(),
    )
    engine = InMemoryClaimEngine(adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=sessions)})
    await engine.upsert(ClaimKind.SESSION, session.id)
    [first] = await engine.claim_due("wrk-a", max_count=10)
    first_outcome = await run_one_session_turn(first, deps)  # the park outcome; its release is never called: rolled back

    seen = {
        "first_outcome_parks": first_outcome.park is not None,
        "row_after_park_turn": await sessions.get(session.id),
        "task_states": {n: (await tasks.get(f"{session.id}/x:tool:0:{n}")).state.value for n in (1, 2, 3)},
    }
    engine._leases[(ClaimKind.SESSION, session.id)].expires_at = _now() - timedelta(seconds=1)
    [second] = await engine.claim_due("wrk-b", max_count=10)
    seen["second_outcome"] = await run_one_session_turn(second, deps)
    seen["row_after_reclaim"] = await sessions.get(session.id)
    records = [json.loads(line) for line in io.read_lines(session.id)]
    seen["kinds_after_reclaim"] = [r["kind"] for r in records]
    seen["error_record"] = next((r["payload"] for r in records if r["kind"] == SessionMessageKind.ERROR.value), None)
    seen["builds"] = len(builds)
    return seen


@pytest.mark.asyncio
async def test_scenario_a_tool_wait_park_release_that_rolled_back_left_the_batch_and_no_park_columns():
    seen = await _tool_wait_park_release_abandoned_then_reclaimed()

    assert seen["first_outcome_parks"] is True
    row = seen["row_after_park_turn"]
    assert (row.status, row.turn_no, row.parked_status) == (SessionStatus.RUNNING, 0, None)
    assert seen["task_states"] == {1: "queued", 2: "queued", 3: "done"}, "the batch's ToolCallTask rows are durable"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_BUG_3212_FLAG_ON)
@pytest.mark.asyncio
async def test_a_re_claim_after_a_rolled_back_tool_wait_park_release_does_not_fail_the_session():
    seen = await _tool_wait_park_release_abandoned_then_reclaimed()

    error = (seen["error_record"] or {}).get("title")
    assert seen["row_after_reclaim"].status is not SessionStatus.ENDED, (
        f"the re-claim ended the session {seen['row_after_reclaim'].ended_reason!r}: {error}: "
        f"{(seen['error_record'] or {}).get('message')}"
    )
    assert seen["kinds_after_reclaim"].count(SessionMessageKind.TOOL_CALL.value) == 3, "the batch was recorded twice"
    assert seen["kinds_after_reclaim"].count(SessionMessageKind.YIELDED.value) == 1, "the park was recorded twice"


# ---------------------------------------------------------------------------
# 425b: a RESUME release abandoned before its commit (an ask_user park resumed through the real pool)
# ---------------------------------------------------------------------------


async def _resume_release_abandoned_then_reclaimed(monkeypatch) -> dict:
    sid, tcid = "sess-resume-rb", "tc-ask-1"
    storage = _FakeStorageProvider()
    sessions = storage.get_storage(WorkspaceSession)
    engine = _build_engine(sessions)
    pool = _build_pool(storage, engine)
    pool._release_timeout_seconds = 0.3
    pool._release_probe_timeout_seconds = 0.3
    assistant = Message(
        role="assistant", parts=[ToolCallPart(id=tcid, name="_misc__ask_user", arguments={"prompt": "name?"})]
    )
    await sessions.create(_make_resumable_session(
        sid, tool_name="ask_user", tool_call_id=tcid, resume_event_payload={"response": "Alice"},
        llm_messages=[assistant.model_dump(mode="json")],
    ))
    executor = _RecordingExecutor()
    monkeypatch.setattr(pool, "_load_workspace_for_persist", lambda _w: _async_return(_NoopPersist()))
    monkeypatch.setattr(pool, "_build_agent_executor", lambda _s, _w: _async_return(executor))

    real_release = engine.release
    abandon = {"left": 1}

    async def release(lease, *, outcome):
        if abandon["left"]:
            abandon["left"] -= 1
            await asyncio.Event().wait()  # never answers; the real release (and its on_release) never runs
        return await real_release(lease, outcome=outcome)

    engine.release = release  # type: ignore[method-assign]

    await engine.mark_resumable(ClaimKind.SESSION, sid)
    [first] = await engine.claim_due("wrk-engine-resume", max_count=10)
    await pool._run_engine_session(first)
    seen = {
        "abandoned": 1 - abandon["left"],
        "row_after_resume": await sessions.get(sid),
        "injected_after_resume": len(executor.injected),
        "lease_holder": engine._leases[(ClaimKind.SESSION, sid)].claimed_by,
    }
    engine._leases[(ClaimKind.SESSION, sid)].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    [second] = await engine.claim_due("wrk-engine-resume-2", max_count=10)
    await pool._run_engine_session(second)
    seen["row_after_reclaim"] = await sessions.get(sid)
    seen["injected_after_reclaim"] = len(executor.injected)
    return seen


@pytest.mark.asyncio
async def test_scenario_a_resume_release_that_rolled_back_left_the_park_resumable_after_the_handler_ran(monkeypatch):
    seen = await _resume_release_abandoned_then_reclaimed(monkeypatch)

    assert seen["abandoned"] == 1, "the resume release was not the abandoned one"
    assert seen["row_after_resume"].parked_status == "resumable", "the park was cleared by the handler, not the release"
    assert seen["injected_after_resume"] == 1, "the handler did not run before the release"
    assert seen["lease_holder"] == "wrk-engine-resume", "the old worker's lease should still be claimed"


@pytest.mark.asyncio
async def test_a_re_claim_after_a_rolled_back_resume_release_does_not_run_the_handler_again(monkeypatch):
    seen = await _resume_release_abandoned_then_reclaimed(monkeypatch)

    assert seen["injected_after_reclaim"] == 1, (
        f"the resume handler ran {seen['injected_after_reclaim']} times: inject_resume_messages appended the operator's "
        "answer again (and an approved tool would execute again)"
    )
