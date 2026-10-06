"""A session turn that COMPLETED is not run again when its release never committed.

Everything a completed turn does is committed piecemeal BEFORE its release (records, status, ``last_seq``, the drain
cursor); only ``turn_no`` and ``last_turn_at`` are written inside the release transaction
(``SessionClaimAdapter.on_release``). When that release is abandoned at the pool's bound (or raises) it rolls back, the
lease is left claimed until it expires, and the next claim reaches ``run_one_session_turn`` again. That re-claim must
not call the model a second time for a turn that already answered.

The worlds here run the REAL ``WorkerPool`` handler and the REAL ``run_one_session_turn`` over the in-memory claim
engine, with a scripted executor whose every ``invoke()`` is one model call. "Rolled back" is modelled the way
``tests/worker/test_release_timeout_outcome.py`` models a release that hung before committing: the wrapped
``engine.release`` awaits forever WITHOUT calling the real one. The in-memory release has no transaction (it runs
``on_release`` and then drops the lease, ``primer/claim/in_memory.py``), so never calling it is exactly the state a
Postgres rollback leaves: the lease still claimed by the old worker and the row's ``turn_no`` not bumped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

import primer.observability.metrics as metrics
from primer.bus.in_memory import InMemoryEventBus
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.chat import Done, TextDelta
from primer.model.except_ import NotFoundError
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.scheduler.in_memory import InMemoryScheduler
from primer.session.compaction import compact_session
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.mutation_lock import session_lifecycle_lock
from primer.session.rewind import append_rewind_marker
from primer.session.turns import has_open_turn
from primer.tap.delta import KIND_TEXT, part_id
from primer.worker.pool import WorkerPool

from tests.conftest import _FakeStorageProvider

SID = "s-reclaim"
WS = "ws-reclaim"
KEY = (ClaimKind.SESSION, SID)


async def _until(predicate, message: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


class _Workspace:
    """The slice of a workspace the turn touches: ``messages.jsonl`` and the state files, in memory."""

    state_path = ".state"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.fail_message_reads = False

    def messages_path(self, session_id: str) -> str:
        return f"{self.state_path}/sessions/{session_id}/messages.jsonl"

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = self.messages_path(session_id)
        self.files[path] = self.files.get(path, b"") + line

    async def append_state_line(self, path: str, line: bytes) -> None:
        self.files[path] = self.files.get(path, b"") + line

    async def read_file(self, path: str) -> bytes:
        if self.fail_message_reads and path.endswith("/messages.jsonl"):
            raise OSError("the workspace volume is not answering")
        if path not in self.files:
            raise NotFoundError(path)
        return self.files[path]

    async def get_session(self, session_id: str):
        return None  # no on-disk slot: wake_session still writes the USER_INPUT record

    def lines(self, session_id: str = SID) -> list[str]:
        raw = self.files.get(self.messages_path(session_id), b"")
        return [ln for ln in raw.decode().splitlines() if ln.strip()]

    def records(self, session_id: str = SID) -> list[dict]:
        return [json.loads(ln) for ln in self.lines(session_id)]


class _Registry:
    def __init__(self, ws: _Workspace) -> None:
        self.ws = ws

    async def get_workspace(self, workspace_id: str):
        return self.ws

    async def check_workspace_allowed(self, workspace_id: str) -> None:
        """The pool asks this before it resumes a park (ticket 01a1072f); this deployment refuses nothing."""
        return None


class _CountingExecutor:
    """One ``invoke()`` is one model call. It records which user message it answered (the last USER_INPUT in the
    log when it was called) and the turn_no it runs at, streams a reply and ends with a clean stop, so the session
    rests WAITING. With ``world.stop_turns`` it instead waits for the Stop it was bound to, as the agent loop does."""

    last_done_reason = "stop"

    def __init__(self, world: "_World", session: WorkspaceSession) -> None:
        self._world = world
        self._session = session
        self._interrupt: asyncio.Event | None = None
        self.was_interrupted = False

    def bind_interrupt_event(self, event: asyncio.Event | None) -> None:
        self._interrupt = event

    async def invoke(self, messages, **kwargs):
        user_inputs = [
            r for r in self._world.ws.records() if r.get("kind") == SessionMessageKind.USER_INPUT.value
        ]
        answered = user_inputs[-1]["payload"].get("text") if user_inputs else None
        self._world.llm_calls.append(answered)
        self._world.call_turn_nos.append(self._session.turn_no)
        yield TextDelta(text=f"reply to {answered}", index=0)
        if self._world.stop_turns:
            assert self._interrupt is not None
            await asyncio.wait_for(self._interrupt.wait(), 5.0)
            self.was_interrupted = True
            return
        yield Done(stop_reason="stop", raw_reason="stop")


class _World:
    """One session, one in-memory engine, any number of pools (one per worker id) sharing it."""

    def __init__(self) -> None:
        self.storage = _FakeStorageProvider()
        self.sessions = self.storage.get_storage(WorkspaceSession)
        self.ws = _Workspace()
        self.registry = _Registry(self.ws)
        self.engine = InMemoryClaimEngine(
            adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=self.sessions)},
        )
        self.scheduler = InMemoryScheduler()
        self.bus = InMemoryEventBus()
        self.llm_calls: list[str | None] = []
        self.call_turn_nos: list[int] = []
        self.builds = 0
        self.stop_turns = False
        # How many of the next releases are abandoned: they never reach the real release (= rolled back).
        self.abandon_next_releases = 0
        self.abandoned = 0
        self.outcomes: list[ReleaseOutcome] = []   # every outcome handed to engine.release, abandoned or not
        self.pools: list[WorkerPool] = []

        real_release = self.engine.release

        async def release(lease, *, outcome):
            self.outcomes.append(outcome)
            if self.abandon_next_releases > 0:
                self.abandon_next_releases -= 1
                self.abandoned += 1
                await asyncio.Event().wait()   # never answers; the real release (and its on_release) never runs
            return await real_release(lease, outcome=outcome)

        self.engine.release = release  # type: ignore[method-assign]

    async def start(self) -> None:
        await self.scheduler.initialize()
        await self.bus.initialize()

    async def close(self) -> None:
        for pool in self.pools:
            for task in list(pool._turn_tasks):
                task.cancel()
            await asyncio.gather(*pool._turn_tasks, return_exceptions=True)
        await self.scheduler.aclose()
        await self.bus.aclose()

    def pool(self, worker_id: str) -> WorkerPool:
        pool = WorkerPool(
            config=WorkerConfig(concurrency=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5),
            scheduler=self.scheduler, storage=self.storage,
            workspace_registry=self.registry,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            event_bus=self.bus,
            engine=self.engine,
        )
        pool._worker_id = worker_id
        pool._dispatch = {ClaimKind.SESSION: pool._run_engine_session}
        pool._release_timeout_seconds = 0.3
        pool._release_probe_timeout_seconds = 0.3

        async def build_executor(session: WorkspaceSession):
            self.builds += 1
            return _CountingExecutor(self, session)

        pool._build_session_executor = build_executor  # type: ignore[method-assign]
        self.pools.append(pool)
        return pool

    def wake_deps(self) -> SessionWakeDeps:
        return SessionWakeDeps(
            storage_provider=self.storage, scheduler=self.scheduler, claim_engine=self.engine,
            workspace_registry=self.registry, event_bus=self.bus,
        )

    async def create_session(self, **fields) -> WorkspaceSession:
        sess = WorkspaceSession(
            id=SID, workspace_id=WS, binding=AgentSessionBinding(agent_id="ag-1"),
            status=SessionStatus.CREATED, created_at=datetime.now(UTC), **fields,
        )
        return await self.sessions.create(sess)

    async def steer(self, text: str) -> None:
        """A human message, exactly as the steer route sends it (USER_INPUT, claimable, last_seq, RUNNING, upsert)."""
        await wake_session(
            workspace_id=WS, session_id=SID, instruction=text, human_intent=True, deps=self.wake_deps(),
        )

    async def claim_and_run(self, pool: WorkerPool) -> int:
        """One claim cycle on ``pool``: claim what is due, run it, wait until the pool is idle. Returns the count."""
        leases = await self.engine.claim_due(pool._worker_id, max_count=10)
        if leases:
            pool._reserve_and_dispatch(leases)
            await _until(lambda: not pool._in_flight, "the claimed turn never finished")
        return len(leases)

    def expire_lease(self) -> None:
        self.engine._leases[KEY].expires_at = datetime.now(UTC) - timedelta(seconds=1)

    async def row(self) -> WorkspaceSession:
        row = await self.sessions.get(SID)
        assert row is not None
        return row


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_test()
    yield
    metrics.reset_for_test()


def _noops() -> float:
    return metrics.session_completed_turn_noop_total._value.get()


@pytest.fixture
async def world():
    w = _World()
    await w.start()
    try:
        yield w
    finally:
        await w.close()


async def _complete_a_turn_whose_release_rolled_back(world: _World) -> WorkerPool:
    """A human message, a turn on worker A that COMPLETES, and A's release abandoned at the bound."""
    await world.create_session()
    await world.steer("first")
    pool_a = world.pool("wrk-a")
    world.abandon_next_releases = 1
    assert await world.claim_and_run(pool_a) == 1

    assert world.llm_calls == ["first"]
    assert pool_a._release_timeouts_total == 1
    assert pool_a._release_timeouts_committed_total == 0
    assert world.engine._leases[KEY].claimed_by == "wrk-a", "the rolled-back release must leave the lease claimed"
    row = await world.row()
    assert row.turn_no == 0, "turn_no is written only inside the release transaction, which rolled back"
    assert row.status == SessionStatus.WAITING
    assert row.turn_status == "idle"
    assert not has_open_turn(world.ws.lines(), cursor=0), "the turn completed: its DONE is in the log"
    return pool_a


@pytest.mark.asyncio
async def test_a_completed_turn_whose_release_rolled_back_is_not_run_again(world):
    await _complete_a_turn_whose_release_rolled_back(world)

    world.expire_lease()
    assert await world.claim_and_run(world.pool("wrk-b")) == 1

    assert world.llm_calls == ["first"], f"the completed turn called the model again: {world.llm_calls}"
    assert world.builds == 1, "the no-op claim built an executor"
    assert KEY not in world.engine._leases
    row = await world.row()
    assert row.turn_no == 1, "the lost bump is applied exactly once"
    assert row.completed_turn_no == 0, "the marker now trails turn_no: later claims run normally"
    assert row.turn_status == "idle" and row.status == SessionStatus.WAITING
    assert _noops() == 1


@pytest.mark.asyncio
async def test_a_steer_queued_after_the_rolled_back_completion_runs_exactly_once(world):
    await _complete_a_turn_whose_release_rolled_back(world)
    await world.steer("second")      # the lease is still claimed by A: the upsert only touches its priority
    assert world.engine._leases[KEY].claimed_by == "wrk-a"

    world.expire_lease()
    pool_b = world.pool("wrk-b")
    for _ in range(3):              # claim, release, claim again: until nothing is left to claim
        if not await world.claim_and_run(pool_b):
            break

    assert len(world.llm_calls) == 2, f"expected the first turn and ONE answer to the steer: {world.llm_calls}"
    assert world.llm_calls[-1] == "second", "the last model call answers the steer"
    assert not has_open_turn(world.ws.lines(), cursor=0), "every user message has its closing record"
    assert await world.engine.claim_due("wrk-c", max_count=10) == []
    assert KEY not in world.engine._leases


@pytest.mark.asyncio
async def test_a_turn_that_crashed_mid_turn_is_run_again_exactly_once(world):
    """No completion: the row is stuck ``running``, the USER_INPUT is the last record, the cursor is behind it, and
    the dead worker's lease has expired. The take-over runs the turn once."""
    await world.create_session()
    await world.steer("crashed")
    row = await world.row()
    assert row.last_seq == 1 and row.next_unprocessed_seq <= row.last_seq
    await world.sessions.update(row.model_copy(update={
        "turn_status": "running", "turn_started_at": datetime.now(UTC),
    }))
    assert await world.engine.claim_due("wrk-dead", max_count=10)    # the worker that died holding it
    world.expire_lease()

    assert await world.claim_and_run(world.pool("wrk-b")) == 1

    assert world.llm_calls == ["crashed"]
    assert KEY not in world.engine._leases
    row = await world.row()
    assert row.turn_no == 1
    assert row.turn_status == "idle"
    assert not has_open_turn(world.ws.lines(), cursor=0)


# ---------------------------------------------------------------------------
# The guard's companions (the lead's rulings, note 01a10b05 section 12)
# ---------------------------------------------------------------------------


async def _claim_until_idle(world: _World, pool: WorkerPool, cycles: int = 5) -> None:
    """Claim and run on ``pool`` until nothing is left to claim (each no-op that re-arms costs one cycle)."""
    for _ in range(cycles):
        if not await world.claim_and_run(pool):
            return
    raise AssertionError(f"still claimable after {cycles} cycles")


def _reply_part_ids(world: _World) -> list[str]:
    return [
        r["payload"]["part_id"] for r in world.ws.records()
        if r.get("kind") == SessionMessageKind.ASSISTANT_TOKEN.value
    ]


@pytest.mark.asyncio
async def test_a_steer_after_the_rolled_back_completion_runs_once_at_the_next_turn_no(world):
    """Marker + queued steer: the re-claim no-ops (its release applies the lost bump to N+1) and re-arms; the
    steer's turn runs ONCE, at N+1, so its ids cannot collide with the completed turn's; turn_no ends at N+2."""
    await _complete_a_turn_whose_release_rolled_back(world)
    await world.steer("second")
    world.expire_lease()
    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["first", "second"]
    assert world.call_turn_nos == [0, 1]
    assert _reply_part_ids(world) == [part_id(None, KIND_TEXT, 0), part_id(None, KIND_TEXT, 1)]
    row = await world.row()
    assert row.turn_no == 2 and row.completed_turn_no == 1
    assert _noops() == 1
    assert KEY not in world.engine._leases


@pytest.mark.asyncio
async def test_a_stopped_turn_whose_release_rolled_back_is_not_run_again(world):
    """A Stop rests the session WAITING; re-running the turn the user stopped is the worst form of the bug."""
    await world.create_session()
    await world.steer("first")
    row = await world.row()
    await world.sessions.update(row.model_copy(update={"interrupt_requested": True}))   # the Stop
    world.stop_turns = True
    world.abandon_next_releases = 1
    assert await world.claim_and_run(world.pool("wrk-a")) == 1
    row = await world.row()
    assert row.status == SessionStatus.WAITING and row.turn_no == 0 and row.completed_turn_no == 0
    assert [r["kind"] for r in world.ws.records()][-1] == SessionMessageKind.CANCELLED.value

    world.expire_lease()
    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["first"], "the stopped turn was run again"
    assert (await world.row()).turn_no == 1
    assert KEY not in world.engine._leases


@pytest.mark.asyncio
async def test_the_noop_is_idempotent_when_its_own_release_is_lost_too(world):
    """The no-op's release abandoned twice: each re-claim no-ops again (still no model call); the first release that
    commits applies the bump once, and after it claims run normally."""
    await _complete_a_turn_whose_release_rolled_back(world)
    world.abandon_next_releases = 2
    for worker in ("wrk-b", "wrk-c"):
        world.expire_lease()
        assert await world.claim_and_run(world.pool(worker)) == 1
        assert world.engine._leases[KEY].claimed_by == worker
        assert (await world.row()).turn_no == 0
    world.expire_lease()
    assert await world.claim_and_run(world.pool("wrk-d")) == 1

    assert world.llm_calls == ["first"] and world.builds == 1
    assert (await world.row()).turn_no == 1
    assert _noops() == 3
    assert KEY not in world.engine._leases

    await world.steer("later")              # once the bump committed, the next turn runs normally
    await _claim_until_idle(world, world.pool("wrk-e"))
    assert world.llm_calls == ["first", "later"]
    assert (await world.row()).turn_no == 2
    assert _noops() == 3


async def _completed_turn_then_stale_snapshot(world: _World) -> WorkspaceSession:
    """Turn 0 completes and its release COMMITS (turn_no 1). Returns what a whole-document writer that read the row
    after the marker and before the release committed holds: the same row with turn_no 0."""
    await world.create_session()
    await world.steer("first")
    assert await world.claim_and_run(world.pool("wrk-a")) == 1
    row = await world.row()
    assert (row.turn_no, row.completed_turn_no, row.turn_status) == (1, 0, "idle")
    assert KEY not in world.engine._leases
    return row.model_copy(update={"turn_no": 0, "last_turn_at": None})


@pytest.mark.asyncio
async def test_a_stale_revert_of_turn_no_then_a_wake_answers_the_steer_exactly_once(world):
    stale = await _completed_turn_then_stale_snapshot(world)
    await world.sessions.update(stale)      # the stale whole-document write lands after the release
    await world.steer("second")             # then a human writes

    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["first", "second"], "the steer must be answered exactly once"
    assert _noops() == 1
    assert not has_open_turn(world.ws.lines(), cursor=0)


@pytest.mark.asyncio
async def test_a_stale_revert_of_turn_no_claimable_last_seq_and_cursor_over_a_wake_is_caught_by_the_log(world):
    """The double race: a wake commits (USER_INPUT, claimable, last_seq, lease), THEN the stale writer puts back
    turn_no, turn_status=idle, last_seq and the cursor. Only the log shows the unanswered input: the no-op arms it
    (turn_status-only patch to claimable) and the pool re-arms, so it is answered exactly once."""
    stale = await _completed_turn_then_stale_snapshot(world)
    await world.steer("second")
    await world.sessions.update(stale)
    row = await world.row()
    assert (row.turn_no, row.turn_status) == (0, "idle")
    assert row.next_unprocessed_seq > row.last_seq, "cursor past last_seq: a cursor <= last_seq test misses this"

    pool_b = world.pool("wrk-b")
    assert await world.claim_and_run(pool_b) == 1           # the no-op
    assert world.llm_calls == ["first"]
    row = await world.row()
    assert row.turn_status == "claimable" and row.turn_no == 1
    assert world.engine._leases[KEY].claimed_by is None, "the pool re-armed a fresh lease"

    await _claim_until_idle(world, pool_b)
    assert world.llm_calls == ["first", "second"]
    assert not has_open_turn(world.ws.lines(), cursor=0)


@pytest.mark.asyncio
async def test_a_stale_revert_after_the_turn_starts_reading_is_decided_from_the_fresh_locked_row(world):
    """The guard decides from a row read under the lifecycle lock, not from the row the turn read at its top: a
    stale revert of turn_no that lands in between makes this claim a no-op (no model call, nothing to answer)."""
    stale = await _completed_turn_then_stale_snapshot(world)
    await world.engine.upsert(ClaimKind.SESSION, SID)        # a /resume of the session at rest
    pool_b = world.pool("wrk-b")
    async with session_lifecycle_lock().acquire(SID):
        leases = await world.engine.claim_due("wrk-b", max_count=10)
        pool_b._reserve_and_dispatch(leases)
        await asyncio.sleep(0.2)                              # the turn has read the row and waits on the lock
        assert world.builds == 1
        await world.sessions.update(stale)
    await _until(lambda: not pool_b._in_flight, "the claimed turn never finished")

    assert world.llm_calls == ["first"]
    assert _noops() == 1
    assert (await world.row()).turn_no == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["compaction", "rewind"])
async def test_a_marker_record_after_the_rolled_back_completion_makes_no_model_call(world, marker):
    """Compaction and rewind append a marker and raise last_seq without moving the cursor, so the cursor sits behind
    last_seq with nothing unanswered. The re-claim must not take that for work."""
    await _complete_a_turn_whose_release_rolled_back(world)
    row = await world.row()
    if marker == "compaction":
        class _Summary:
            summary_text = "the conversation so far"
            tokens_before, tokens_after = 100, 10

        async def run(_history):
            return _Summary()

        outcome = await compact_session(row=row, workspace_io=world.ws, history=[], run_compaction=run)
        marker_seq = outcome.compaction_marker_seq
    else:
        marker_seq = await append_rewind_marker(
            workspace_io=world.ws, session_id=SID, start_seq=row.last_seq, to_seq=1, actor="user",
        )
    await world.sessions.update(row.model_copy(update={"last_seq": marker_seq}))   # what the route writes
    row = await world.row()
    assert row.next_unprocessed_seq <= row.last_seq

    world.expire_lease()
    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["first"]
    row = await world.row()
    assert row.turn_no == 1 and row.turn_status == "idle"
    assert _noops() == 1


@pytest.mark.asyncio
async def test_a_stale_marker_does_not_swallow_a_crash_takeover(world):
    """A stale marker (completed_turn_no == turn_no with no release pending) and a turn that crashed mid-turn: the
    first claim no-ops but arms the unanswered input, and the turn then runs exactly once."""
    await world.create_session(turn_no=1, completed_turn_no=1)
    await world.steer("crashed")
    row = await world.row()
    await world.sessions.update(row.model_copy(update={"turn_status": "running", "turn_started_at": datetime.now(UTC)}))
    assert await world.engine.claim_due("wrk-dead", max_count=10)
    world.expire_lease()

    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["crashed"]
    assert world.call_turn_nos == [2]
    assert _noops() == 1
    assert (await world.row()).turn_no == 3
    assert not has_open_turn(world.ws.lines(), cursor=0)


@pytest.mark.asyncio
async def test_a_stale_marker_and_a_resume_at_rest_make_no_model_call(world):
    """A session at rest (its turn answered, WAITING, idle) with a stale marker, then /resume: nothing to answer, so
    no model call (before the guard this re-ran the last turn)."""
    await world.create_session()
    await world.steer("first")
    assert await world.claim_and_run(world.pool("wrk-a")) == 1
    row = await world.row()
    await world.sessions.update(row.model_copy(update={"completed_turn_no": row.turn_no}))   # stale
    async with session_lifecycle_lock().acquire(SID):        # what POST .../resume does to a WAITING row
        row = await world.row()
        await world.sessions.update(row.model_copy(update={"status": SessionStatus.RUNNING}))
        await world.engine.upsert(ClaimKind.SESSION, SID)

    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["first"]
    assert _noops() == 1
    assert (await world.row()).turn_no == 2


@pytest.mark.asyncio
async def test_a_log_that_cannot_be_read_runs_the_turn(world):
    """The guard cannot see whether input is unanswered: it runs the turn (the behaviour before the guard), it never
    swallows one."""
    await _complete_a_turn_whose_release_rolled_back(world)
    world.ws.fail_message_reads = True
    world.expire_lease()
    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["first", "first"]
    assert _noops() == 0


@pytest.mark.asyncio
async def test_a_queued_steer_the_skipped_checkpoint_left_is_realized_and_answered_once(world):
    """A crash between the marker and the drain checkpoint leaves a queued (pending) steer, not yet a USER_INPUT, and
    the row idle. The no-op runs the checkpoint the turn would have run: the steer is realized through wake_session
    (USER_INPUT, claimable) and answered exactly once. Arming the row without realizing it would first run a turn with
    nothing new to answer (a second reply to the completed turn) and only then the steer."""
    from primer.session.pending_messages import store_pending_steer

    await _complete_a_turn_whose_release_rolled_back(world)
    await store_pending_steer(
        storage_provider=world.storage, session=await world.row(), text="queued", workspace_registry=world.registry,
    )
    world.expire_lease()
    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["first", "queued"]
    assert world.call_turn_nos == [0, 1]
    assert _noops() == 1
    assert not has_open_turn(world.ws.lines(), cursor=0)


async def _pending_texts(world: _World) -> list[str]:
    from primer.model.storage import OffsetPage
    from primer.model.workspace_session import PendingSessionMessage

    page = await world.storage.get_storage(PendingSessionMessage).list(OffsetPage(offset=0, length=50))
    return [p.get("text") for row in page.items for p in row.parts]


async def _store_pending(world: _World, text: str) -> None:
    from primer.session.pending_messages import store_pending_steer

    await store_pending_steer(
        storage_provider=world.storage, session=await world.row(), text=text, workspace_registry=world.registry,
    )


@pytest.mark.asyncio
async def test_an_armed_steer_and_a_pending_one_after_a_rolled_back_completion_are_answered_in_order(world):
    """The no-op realizes a queued steer ONLY when nothing is armed. Here 'second' is armed (claimable) and 'third' is
    still pending: the no-op must leave 'third' queued, the next turn answers 'second' and its checkpoint realizes
    'third', which the turn after answers. Realizing 'third' in the no-op too would put two USER_INPUTs before one
    turn: 'second' would never get its own answer and the log would keep an open turn for good (every later steer is
    then routed to pending and the session sticks)."""
    await _complete_a_turn_whose_release_rolled_back(world)
    await world.steer("second")
    await _store_pending(world, "third")
    world.expire_lease()

    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["first", "second", "third"]
    assert not has_open_turn(world.ws.lines(), cursor=0), "an input was left without its closing record"
    assert await _pending_texts(world) == []


@pytest.mark.asyncio
async def test_the_noop_that_arms_an_open_input_leaves_a_pending_steer_queued(world):
    """The has_open_turn branch: a stale write put back turn_no, turn_status=idle, last_seq and the cursor over the
    armed steer 'second', and 'third' is pending. The no-op arms 'second' (claimable) and must NOT realize 'third';
    the following turns answer them in order."""
    stale = await _completed_turn_then_stale_snapshot(world)
    await world.steer("second")
    await world.sessions.update(stale)
    await _store_pending(world, "third")
    row = await world.row()
    assert (row.turn_no, row.turn_status) == (0, "idle")

    pool_b = world.pool("wrk-b")
    assert await world.claim_and_run(pool_b) == 1          # the no-op
    assert world.llm_calls == ["first"]
    assert (await world.row()).turn_status == "claimable", "the open input was not armed"
    assert await _pending_texts(world) == ["third"], "the no-op realized a pending steer while an input was open"

    await _claim_until_idle(world, pool_b)
    assert world.llm_calls == ["first", "second", "third"]
    assert not has_open_turn(world.ws.lines(), cursor=0)
    assert await _pending_texts(world) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [SessionStatus.CREATED])
@pytest.mark.parametrize("armed", ["open", "claimable"])
async def test_input_waiting_on_a_row_the_pool_does_not_rearm_runs_the_turn(world, status, armed):
    """A stale marker (completed_turn_no == turn_no with no release pending) on a CREATED row with a USER_INPUT
    waiting, open in the log or already claimable. The pool re-arms only RUNNING/WAITING rows, so a no-op here would
    leave the input with no lease for good: the claim runs the turn instead, one model call. (A PAUSED or ENDED row
    runs no turn: ``test_a_row_paused_or_ended_at_the_guards_first_read_runs_no_turn``.)"""
    await world.create_session(turn_no=1, completed_turn_no=1)
    await world.steer("waiting")                    # USER_INPUT, claimable, RUNNING, a lease
    row = await world.row()
    await world.sessions.update(row.model_copy(update={
        "status": status, "turn_status": "idle" if armed == "open" else "claimable",
    }))
    assert not (await world.row()).pause_requested     # not the pause exit: the guard decides

    await _claim_until_idle(world, world.pool("wrk-b"))

    assert world.llm_calls == ["waiting"], "the waiting input was not answered"
    assert world.call_turn_nos == [1]
    assert _noops() == 0
    assert (await world.row()).turn_no == 2
    assert not has_open_turn(world.ws.lines(), cursor=0)


def _commit_before_the_arming_patch(world: _World, competing: dict) -> list:
    """Another process writes the row just BEFORE the no-op's arming patch runs: ``competing`` is committed as a
    whole-document update from a fresh read (the way the pause and cancel routes write), then the REAL ``patch_if``
    runs against the row it left. Only the first arming patch is preceded by the write. Returns the list each arming
    patch's result is appended to (``None`` is a refusal by its fence)."""
    real_patch_if = world.sessions.patch_if
    results: list = []

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        arming = patch == {"turn_status": "claimable"}
        if arming and not results:
            row = await world.row()
            await world.sessions.update(row.model_copy(update=competing))
        out = await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)
        if arming:
            results.append(out)
        return out

    world.sessions.patch_if = patch_if  # type: ignore[method-assign]
    return results


async def _open_input_over_a_stale_revert(world: _World, **fields) -> None:
    """The has_open_turn branch's state: turn 0 completed and released, a steer 'second' landed, then a stale
    whole-document write put back turn_no 0 (the marker matches again), turn_status idle and the cursor, plus
    ``fields``. The next claim reaches the arming patch."""
    stale = await _completed_turn_then_stale_snapshot(world)
    await world.steer("second")
    await world.sessions.update(stale.model_copy(update=fields))
    row = await world.row()
    assert (row.turn_no, row.completed_turn_no, row.turn_status) == (0, 0, "idle")
    assert has_open_turn(world.ws.lines(), cursor=row.next_unprocessed_seq)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [SessionStatus.RUNNING, SessionStatus.WAITING])
async def test_an_arming_patch_the_row_refuses_runs_the_turn(world, status):
    """The open input could not be armed because the row changed under the patch (another process: a steer that set
    ``claimable``, which the ``turn_status`` fence refuses), and the row read again under the same lock is still
    RUNNING or WAITING: the claim answers the input now instead of releasing with nothing armed."""
    await _open_input_over_a_stale_revert(world, status=status)
    refusals = _commit_before_the_arming_patch(world, {"turn_status": "claimable"})

    await _claim_until_idle(world, world.pool("wrk-b"))

    assert refusals == [None], "the arming patch was not refused"
    assert world.llm_calls == ["first", "second"]
    assert _noops() == 0
    assert not has_open_turn(world.ws.lines(), cursor=0)


_PARK = {"parked_status": "parked", "parked_event_key": "evt-approval", "parked_state": {"tool_call_id": "tc-1"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_status", ["idle", "running"])
@pytest.mark.parametrize("status", [SessionStatus.PAUSED, SessionStatus.ENDED])
async def test_an_arming_patch_refused_on_a_row_another_process_paused_or_ended_runs_no_turn(
    world, status, turn_status,
):
    """Another process pauses or ends the row just BEFORE the arming patch runs (the pause route on a WAITING row, a
    cancel). The patch's ``status`` fence refuses it, so the row keeps the status that write gave it and is NOT left
    ``claimable`` (without that clause the patch would arm a PAUSED or ENDED row). The claim then re-reads the row under
    the same lock and does not run the turn, which would undo the pause or answer on an ended session: PAUSED takes
    the no-op with the pause exit's park-preserving release, so the input waits for /resume; ENDED drops the lease as
    the ENDED exit does, park cleared. Both heal a stale ``running`` as those exits do, both releases apply the lost
    bump once, and both count as a no-op (the guard found the completed turn and released it without the model)."""
    await _open_input_over_a_stale_revert(world, **_PARK)
    competing: dict = {"status": status, "turn_status": turn_status}
    if status == SessionStatus.ENDED:
        competing |= {"ended_reason": "cancelled", "ended_at": datetime.now(UTC)}
    refusals = _commit_before_the_arming_patch(world, competing)

    assert await world.claim_and_run(world.pool("wrk-b")) == 1

    assert refusals == [None], "the status fence did not refuse the arming patch"
    assert world.llm_calls == ["first"], f"the turn ran on a row another process set to {status.value}"
    assert world.builds == 1, "the claim built an executor"
    row = await world.row()
    assert row.status == status, "the row lost the status the other process gave it"
    assert row.turn_status == "idle", f"turn_status is {row.turn_status!r}: armed, or a stale running left"
    assert row.turn_no == 1 and row.completed_turn_no == 0, "the release applies the lost bump exactly once"
    assert row.last_worker_id is None
    park = {k: getattr(row, k) for k in _PARK}
    if status == SessionStatus.PAUSED:
        assert park == _PARK, "the paused row lost its park (the release did not preserve it)"
    else:
        assert row.ended_reason == "cancelled"
        assert park == dict.fromkeys(_PARK), "the ENDED exit's release clears the park"
    assert _noops() == 1
    assert KEY not in world.engine._leases, "the lease was not dropped"
    assert await world.engine.claim_due("wrk-c", max_count=10) == []


@pytest.mark.asyncio
async def test_the_input_a_refused_arming_left_on_a_paused_row_is_answered_once_after_resume(world):
    """The PAUSED no-op leaves the open input for /resume: nothing runs while the row is PAUSED, and after the resume
    the input is answered exactly once, at the bumped turn_no."""
    await _open_input_over_a_stale_revert(world)
    _commit_before_the_arming_patch(world, {"status": SessionStatus.PAUSED})
    assert await world.claim_and_run(world.pool("wrk-b")) == 1
    assert world.llm_calls == ["first"], "the turn ran on a paused row"
    assert await world.engine.claim_due("wrk-c", max_count=10) == []

    async with session_lifecycle_lock().acquire(SID):        # what POST .../resume does to a PAUSED row
        row = await world.row()
        await world.sessions.update(row.model_copy(update={"status": SessionStatus.RUNNING}))
        await world.engine.upsert(ClaimKind.SESSION, SID)
    await _claim_until_idle(world, world.pool("wrk-c"))

    assert world.llm_calls == ["first", "second"]
    assert world.call_turn_nos == [0, 1]
    assert _noops() == 1
    assert (await world.row()).turn_no == 2
    assert not has_open_turn(world.ws.lines(), cursor=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag, expected", [
    ("ended", (SessionStatus.ENDED, "completed")),
    ("cancel_requested", (SessionStatus.ENDED, "cancelled")),
    ("pause_requested", (SessionStatus.PAUSED, None)),
], ids=["ended", "cancel_requested", "pause_requested"])
async def test_the_ended_cancel_and_pause_exits_win_over_the_noop(world, flag, expected):
    """The guard sits AFTER the ENDED, cancel and pause exits. With the marker matching and nothing claimable, a
    no-op taken before them would bump turn_no and arm nothing, stranding the cancel or the pause on a row that is
    neither ENDED nor PAUSED until something else wakes it. The exit must win."""
    await _complete_a_turn_whose_release_rolled_back(world)
    row = await world.row()
    assert row.completed_turn_no == row.turn_no == 0 and row.turn_status == "idle"
    fields = {"status": SessionStatus.ENDED, "ended_reason": "completed"} if flag == "ended" else {flag: True}
    await world.sessions.update(row.model_copy(update=fields))
    world.expire_lease()

    assert await world.claim_and_run(world.pool("wrk-b")) == 1

    row = await world.row()
    assert (row.status, row.ended_reason) == expected, f"the {flag} exit did not run"
    assert _noops() == 0, "the completed-turn no-op ran before the exit"
    assert world.llm_calls == ["first"]
    assert KEY not in world.engine._leases


# ---------------------------------------------------------------------------
# The guard's first read and a gone row (the lead's ruling, task 01a11045)
# ---------------------------------------------------------------------------


def _commit_before_the_guards_first_read(world: _World, competing: dict | None) -> list:
    """Another process writes the row AFTER the turn's own top read (unlocked: the ENDED, cancel and pause exits have
    already passed on the row as it was) and just BEFORE the guard's first fresh read under the lifecycle lock (the
    lock is per-process, so it does not order the two). ``competing`` is committed as a whole-document update from a
    fresh read (the way the pause and cancel routes write), or the row is deleted when it is ``None``; then the REAL
    ``get`` runs. Only the guard's first read is preceded by the write: the read is told apart by the function that
    awaits it. Returns the list the write is recorded in, so a test can tell that it happened."""
    real_get = world.sessions.get
    fired: list = []

    async def get(id, *, conn=None):
        if not fired and sys._getframe(1).f_code.co_name == "_noop_if_turn_already_completed":
            fired.append(competing)
            if competing is None:
                await world.sessions.delete(id)
            else:
                row = await real_get(id)
                await world.sessions.update(row.model_copy(update=competing))
        return await real_get(id, conn=conn)

    world.sessions.get = get  # type: ignore[method-assign]
    return fired


def _delete_at_the_arming_patch(world: _World, *, after_a_refusal: bool) -> list:
    """Another process deletes the row at the arming patch. Without ``after_a_refusal`` the delete commits just BEFORE
    the REAL ``patch_if`` runs, which raises ``NotFoundError`` on the missing row (as both backends do). With it, a
    steer's ``claimable`` commits first, so the REAL patch is refused by its ``turn_status`` fence, and the delete
    commits after the refusal, before the guard reads the row again. Returns the list the arming patch's result (or
    the exception it raised) is appended to."""
    real_patch_if = world.sessions.patch_if
    results: list = []

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        if patch != {"turn_status": "claimable"} or results:
            return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)
        if after_a_refusal:
            row = await world.row()
            await world.sessions.update(row.model_copy(update={"turn_status": "claimable"}))
        else:
            await world.sessions.delete(id)
        try:
            out = await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)
        except NotFoundError as exc:
            results.append(exc)
            raise
        results.append(out)
        if after_a_refusal:
            await world.sessions.delete(id)
        return out

    world.sessions.patch_if = patch_if  # type: ignore[method-assign]
    return results


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_status", ["claimable", "idle", "running"])
@pytest.mark.parametrize("status", [SessionStatus.PAUSED, SessionStatus.ENDED])
async def test_a_row_paused_or_ended_at_the_guards_first_read_runs_no_turn(world, status, turn_status):
    """Another process pauses or ends the row between the turn's top read and the guard's first fresh read. The
    ENDED and pause exits decided from the top read, and nothing re-checks the status after the guard, so the turn used
    to run on that row (its running flip a whole-document update over the stale snapshot) for a queued steer
    (``claimable``: the claimable branch) or an open input (``idle``, or a stale ``running``: the has_open_turn branch).
    The guard applies the rule of a refused arming patch: PAUSED takes the no-op with the pause exit's park-preserving
    release (the input waits for /resume), ENDED drops the lease as the ENDED exit does (park cleared). Both heal a
    stale ``running``, both releases apply the lost bump once, and both count as a no-op."""
    await _open_input_over_a_stale_revert(world, **_PARK)
    competing: dict = {"status": status, "turn_status": turn_status}
    if status == SessionStatus.ENDED:
        competing |= {"ended_reason": "cancelled", "ended_at": datetime.now(UTC)}
    fired = _commit_before_the_guards_first_read(world, competing)

    assert await world.claim_and_run(world.pool("wrk-b")) == 1

    assert fired == [competing], "the competing write did not land before the guard's first read"
    assert world.llm_calls == ["first"], f"the turn ran on a row another process set to {status.value}"
    assert world.builds == 1, "the claim built an executor"
    row = await world.row()
    assert row.status == status, "the row lost the status the other process gave it"
    expected_turn_status = "claimable" if turn_status == "claimable" else "idle"
    assert row.turn_status == expected_turn_status, f"turn_status is {row.turn_status!r}: a stale running was left"
    assert row.turn_no == 1 and row.completed_turn_no == 0, "the release applies the lost bump exactly once"
    assert row.last_worker_id is None
    park = {k: getattr(row, k) for k in _PARK}
    if status == SessionStatus.PAUSED:
        assert world.outcomes[-1] == ReleaseOutcome(success=True, drop_lease=True, preserve_park=True)
        assert park == _PARK, "the paused row lost its park (the release did not preserve it)"
        assert has_open_turn(world.ws.lines(), cursor=row.next_unprocessed_seq), "the input is left for /resume"
    else:
        assert world.outcomes[-1] == ReleaseOutcome(success=True, drop_lease=True)
        assert row.ended_reason == "cancelled"
        assert park == dict.fromkeys(_PARK), "the ENDED exit's release clears the park"
    assert _noops() == 1
    assert KEY not in world.engine._leases, "the lease was not dropped"
    assert await world.engine.claim_due("wrk-c", max_count=10) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_status", ["claimable", "idle"])
async def test_the_input_left_on_a_row_paused_at_the_guards_first_read_is_answered_once_after_resume(
    world, turn_status,
):
    """The PAUSED no-op of the first read leaves the input for /resume: nothing runs while the row is PAUSED, and
    after the resume the input is answered exactly once, at the bumped turn_no."""
    await _open_input_over_a_stale_revert(world)
    _commit_before_the_guards_first_read(world, {"status": SessionStatus.PAUSED, "turn_status": turn_status})
    assert await world.claim_and_run(world.pool("wrk-b")) == 1
    assert world.llm_calls == ["first"], "the turn ran on a paused row"
    assert await world.engine.claim_due("wrk-c", max_count=10) == []

    async with session_lifecycle_lock().acquire(SID):        # what POST .../resume does to a PAUSED row
        row = await world.row()
        await world.sessions.update(row.model_copy(update={"status": SessionStatus.RUNNING}))
        await world.engine.upsert(ClaimKind.SESSION, SID)
    await _claim_until_idle(world, world.pool("wrk-c"))

    assert world.llm_calls == ["first", "second"]
    assert world.call_turn_nos == [0, 1]
    assert _noops() == 1
    assert (await world.row()).turn_no == 2
    assert not has_open_turn(world.ws.lines(), cursor=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["first_read", "arming_patch"])
async def test_the_paused_noop_clears_a_stale_interrupt_requested(world, where):
    """The PAUSED no-op clears ``interrupt_requested`` under the lock, as the pause exit does: a stale Stop carried
    into the PAUSED row would leak into the turn that eventually resumes it and could downgrade a later genuine Cancel
    to a Stop. Both paths: PAUSED at the guard's first read, and PAUSED under a refused arming patch."""
    await _open_input_over_a_stale_revert(world)
    competing = {"status": SessionStatus.PAUSED, "interrupt_requested": True}
    if where == "first_read":
        fired = _commit_before_the_guards_first_read(world, competing)
    else:
        fired = _commit_before_the_arming_patch(world, competing)

    assert await world.claim_and_run(world.pool("wrk-b")) == 1

    assert len(fired) == 1, "the competing write did not land"
    assert world.llm_calls == ["first"], "the turn ran on a paused row"
    assert world.outcomes[-1] == ReleaseOutcome(success=True, drop_lease=True, preserve_park=True)
    row = await world.row()
    assert row.status == SessionStatus.PAUSED
    assert row.interrupt_requested is False, "a stale Stop was left on the PAUSED row"


@pytest.mark.asyncio
async def test_an_arming_patch_refused_on_a_row_another_process_reset_to_created_runs_the_turn(world):
    """CREATED after a refused arming patch (a reset or a stale write under the patch): the pool does not re-arm a
    CREATED row, so the claim answers the input now, as before the guard."""
    await _open_input_over_a_stale_revert(world)
    refusals = _commit_before_the_arming_patch(world, {"status": SessionStatus.CREATED})

    await _claim_until_idle(world, world.pool("wrk-b"))

    assert refusals == [None], "the status fence did not refuse the arming patch"
    assert world.llm_calls == ["first", "second"]
    assert _noops() == 0
    assert not has_open_turn(world.ws.lines(), cursor=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["first_read", "before_the_arming_patch", "after_a_refused_arming_patch"])
async def test_a_row_gone_under_the_guard_drops_the_lease_and_runs_no_turn(world, where, caplog):
    """The row is deleted under the guard: before its first read, just before the arming patch (which raises
    ``NotFoundError``), or after a refused arming patch and before the re-read. Running the turn on a row that is gone
    answers nothing anyone can see; the claim mirrors the vanished-before-dispatch exit of ``run_one_session_turn``
    (``success=False, drop_lease=True``) and logs that the row vanished, not that it is running the turn. It is not a
    completed-turn no-op, so it is not counted as one."""
    await _open_input_over_a_stale_revert(world)
    if where == "first_read":
        fired = _commit_before_the_guards_first_read(world, None)
    else:
        fired = _delete_at_the_arming_patch(world, after_a_refusal=where == "after_a_refused_arming_patch")

    with caplog.at_level(logging.WARNING, logger="primer.session.dispatch"):
        assert await world.claim_and_run(world.pool("wrk-b")) == 1

    assert len(fired) == 1, "the delete did not land"
    if where == "before_the_arming_patch":
        assert isinstance(fired[0], NotFoundError), "the arming patch on the missing row did not raise NotFoundError"
    if where == "after_a_refused_arming_patch":
        assert fired == [None], "the arming patch was not refused"
    assert await world.sessions.get(SID) is None
    assert world.llm_calls == ["first"], "the turn ran on a row that is gone"
    assert world.builds == 1, "the claim built an executor"
    assert world.outcomes[-1] == ReleaseOutcome(success=False, drop_lease=True)
    assert _noops() == 0, "a gone row is not a completed-turn no-op"
    assert KEY not in world.engine._leases, "the lease was not dropped"
    messages = [r.getMessage() for r in caplog.records if r.name == "primer.session.dispatch"]
    assert any("vanished" in m for m in messages), messages
    assert not any("running the turn" in m for m in messages), messages


@pytest.mark.asyncio
async def test_an_arming_patch_that_raises_a_storage_error_runs_the_turn(world, caplog):
    """A storage error from the arming patch is not a gone row (that is ``NotFoundError``): the claim cannot tell what
    the row holds, so it answers the input now, as before the guard, and logs that it does."""
    await _open_input_over_a_stale_revert(world)
    real_patch_if = world.sessions.patch_if
    raised: list = []

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):
        if patch == {"turn_status": "claimable"} and not raised:
            raised.append(True)
            raise ConnectionError("the database is not answering")
        return await real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    world.sessions.patch_if = patch_if  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="primer.session.dispatch"):
        await _claim_until_idle(world, world.pool("wrk-b"))

    assert raised == [True]
    assert world.llm_calls == ["first", "second"], "the input was not answered"
    assert _noops() == 0
    assert not has_open_turn(world.ws.lines(), cursor=0)
    messages = [r.getMessage() for r in caplog.records if r.name == "primer.session.dispatch"]
    assert any("running the turn" in m for m in messages), messages
