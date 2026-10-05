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
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.turns import has_open_turn
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

    def messages_path(self, session_id: str) -> str:
        return f"{self.state_path}/sessions/{session_id}/messages.jsonl"

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = self.messages_path(session_id)
        self.files[path] = self.files.get(path, b"") + line

    async def append_state_line(self, path: str, line: bytes) -> None:
        self.files[path] = self.files.get(path, b"") + line

    async def read_file(self, path: str) -> bytes:
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
    log when it was called), streams a reply and ends with a clean stop, so the session rests WAITING."""

    last_done_reason = "stop"

    def __init__(self, world: "_World", session: WorkspaceSession) -> None:
        self._world = world
        self._session = session

    async def invoke(self, messages, **kwargs):
        user_inputs = [
            r for r in self._world.ws.records() if r.get("kind") == SessionMessageKind.USER_INPUT.value
        ]
        answered = user_inputs[-1]["payload"].get("text") if user_inputs else None
        self._world.llm_calls.append(answered)
        yield TextDelta(text=f"reply to {answered}", index=0)
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
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "01a10b05: a completed turn whose release rolled back is re-claimed and run_one_session_turn runs a NEW "
        "turn over the same history (a second model call)"
    ),
)
async def test_a_completed_turn_whose_release_rolled_back_is_not_run_again(world):
    await _complete_a_turn_whose_release_rolled_back(world)

    world.expire_lease()
    assert await world.claim_and_run(world.pool("wrk-b")) == 1

    assert world.llm_calls == ["first"], f"the completed turn called the model again: {world.llm_calls}"
    assert KEY not in world.engine._leases
    assert (await world.row()).turn_no == 1, "the lost bump is applied exactly once"


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
