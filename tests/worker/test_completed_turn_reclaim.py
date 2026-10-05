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
import time
from datetime import UTC, datetime, timedelta

import pytest

import primer.observability.metrics as metrics
from primer.bus.in_memory import InMemoryEventBus
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
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
        self.pools: list[WorkerPool] = []

        real_release = self.engine.release

        async def release(lease, *, outcome):
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
