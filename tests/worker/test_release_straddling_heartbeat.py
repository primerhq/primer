"""A lost-lease verdict for a lease the execution has already GIVEN BACK is not delivered.

``_heartbeat_loop`` decides "lost" from what it SENT, then cancels the captured scope of every key the
engine did not confirm. If the execution releases its lease while that heartbeat's round trip is
outstanding, the engine answers "not mine" (the lease is gone, which is exactly what a release does) and
the loop cancelled the task of an execution that had nothing left to preempt except its own TAIL:
``_run_engine_session`` re-arms a queued wake/steer after the release (``_maybe_rearm_session``), and a
cancel landing there leaves the steer with no lease. The window opens whenever the engine answers a
heartbeat AFTER the release committed (on Postgres the heartbeat's UPDATE waits on the release
transaction's row lock and returns no row at commit), and a drain, which now keeps the heartbeat running
through the whole shutdown, makes every such turn eligible, not just one rollout's.

The verdict must still reach an execution that genuinely lost its lease (the second session in each
test), and a user cancel is not part of this change.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.scheduler.in_memory import InMemoryScheduler
from primer.worker.pool import WorkerPool

from tests.conftest import _FakeStorageProvider

WORKER = "wrk-straddle"


def _session(sid: str) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=0,
    )


async def _until(predicate, message: str, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


class _World:
    """Two sessions on a real pool: ``releasing`` ends its turn mid-heartbeat, ``lost`` loses its lease."""

    def __init__(self, monkeypatch) -> None:
        self.storage = _FakeStorageProvider()
        self.sessions = self.storage.get_storage(WorkspaceSession)
        self.engine = InMemoryClaimEngine(
            adapters={ClaimKind.SESSION: SessionClaimAdapter(session_storage=self.sessions)},
        )
        self.scheduler = InMemoryScheduler()
        self.pool = WorkerPool(
            config=WorkerConfig(
                concurrency=4, claim_batch_size=4, heartbeat_interval_seconds=1,
                lease_ttl_seconds=5, poll_interval_seconds=0.1, drain_timeout_seconds=5,
            ),
            scheduler=self.scheduler, storage=self.storage,
            workspace_registry=None,  # type: ignore[arg-type]
            provider_registry=None,  # type: ignore[arg-type]
            engine=self.engine,
        )
        self.pool._worker_id = WORKER
        self.pool._dispatch = {ClaimKind.SESSION: self.pool._run_engine_session}
        self.turn_gate = {"releasing": asyncio.Event(), "lost": asyncio.Event()}
        self.cancelled: dict[str, int] = {"releasing": 0, "lost": 0}   # turn bodies cancelled mid-wait
        self.heartbeat_sent = asyncio.Event()
        self.heartbeat_gate = asyncio.Event()
        self.rearm_entered = asyncio.Event()
        self.rearm_gate = asyncio.Event()
        self.rearm_done = asyncio.Event()
        self.rearm_armed = False   # mark_resumable() in setup also goes through upsert
        self.sent_keys: list[list[tuple[ClaimKind, str]]] = []
        monkeypatch.setattr("primer.worker.pool.run_one_session_turn", self._turn)

        real_heartbeat = self.engine.heartbeat
        real_upsert = self.engine.upsert
        hold_first = {"armed": True}

        async def heartbeat(worker_id, kind_ids):
            self.sent_keys.append(list(kind_ids))
            if hold_first["armed"]:
                hold_first["armed"] = False
                self.heartbeat_sent.set()
                await self.heartbeat_gate.wait()
            return await real_heartbeat(worker_id, kind_ids)

        async def upsert(kind, entity_id, **kw):
            gated = self.rearm_armed and kind is ClaimKind.SESSION and entity_id == "releasing"
            if gated:
                self.rearm_entered.set()
                await self.rearm_gate.wait()
            await real_upsert(kind, entity_id, **kw)
            if gated:
                self.rearm_done.set()

        self.engine.heartbeat = heartbeat  # type: ignore[method-assign]
        self.engine.upsert = upsert  # type: ignore[method-assign]

    async def _turn(self, engine_lease, _deps) -> ReleaseOutcome:
        sid = engine_lease.entity_id
        try:
            await self.turn_gate[sid].wait()
        except asyncio.CancelledError:
            self.cancelled[sid] += 1
            raise
        # A wake_session() steer landed while the turn ran: turn_status is "claimable" and only the
        # post-release re-arm can give it a lease.
        row = await self.sessions.get(sid)
        await self.sessions.update(row.model_copy(update={"turn_status": "claimable"}))
        return ReleaseOutcome(success=True, drop_lease=True)

    async def __aenter__(self) -> "_World":
        await self.scheduler.initialize()
        for sid in ("releasing", "lost"):
            await self.sessions.create(_session(sid))
            await self.engine.mark_resumable(ClaimKind.SESSION, sid)
        leases = await self.engine.claim_due(WORKER, max_count=10)
        assert {lease.entity_id for lease in leases} == {"releasing", "lost"}
        self.pool._reserve_and_dispatch(leases)
        await _until(lambda: len(self.pool._active_scopes) == 2, "both turns did not start")
        self.rearm_armed = True
        self.loop_task = asyncio.create_task(self.pool._heartbeat_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        self.pool._stopping.set()
        self.pool._keepalive_done.set()
        for gate in (self.heartbeat_gate, self.rearm_gate, *self.turn_gate.values()):
            gate.set()
        self.loop_task.cancel()
        await asyncio.gather(self.loop_task, return_exceptions=True)
        for task in list(self.pool._turn_tasks):
            task.cancel()
        await asyncio.gather(*self.pool._turn_tasks, return_exceptions=True)
        await self.scheduler.aclose()


@pytest.mark.asyncio
async def test_a_verdict_for_a_lease_already_released_does_not_cancel_the_rearm(monkeypatch):
    async with _World(monkeypatch) as w:
        await asyncio.wait_for(w.heartbeat_sent.wait(), timeout=5.0)
        # Both keys were sent: the verdict will be about leases that were held when it left.
        assert {k[1] for k in w.sent_keys[0]} == {"releasing", "lost"}
        scope = w.pool._active_scopes[(ClaimKind.SESSION, "releasing")]

        # The first session ends its turn and releases while that heartbeat is outstanding, and is
        # now inside the post-release re-arm. The second genuinely loses its lease.
        w.turn_gate["releasing"].set()
        await asyncio.wait_for(w.rearm_entered.wait(), timeout=5.0)
        await w.engine.delete_lease(ClaimKind.SESSION, "lost")

        w.heartbeat_gate.set()
        # The control: a lease that really was lost still preempts its turn.
        await _until(lambda: w.cancelled["lost"] == 1, "the genuinely lost lease was not preempted")

        w.rearm_gate.set()
        await asyncio.wait_for(w.rearm_done.wait(), timeout=5.0)
        await _until(
            lambda: ("releasing" not in {k[1] for k in w.pool._in_flight}),
            "the releasing session never finished",
        )
        assert scope.cancelled is False   # nothing was delivered to the finished execution
        # The wake steer has its lease back.
        assert await w.engine.has_lease(ClaimKind.SESSION, "releasing")


@pytest.mark.asyncio
async def test_a_verdict_arriving_while_the_release_is_in_flight_does_not_cancel_it(monkeypatch):
    """The verdict can also arrive AFTER the release committed but BEFORE ``engine.release`` returns
    (the post-commit hooks, or the round trip back): the lease is gone, the task is still inside the call."""
    async with _World(monkeypatch) as w:
        real_release = w.engine.release
        release_entered = asyncio.Event()
        release_gate = asyncio.Event()
        released = asyncio.Event()

        async def release(lease, *, outcome):
            await real_release(lease, outcome=outcome)
            if lease.entity_id == "releasing":
                release_entered.set()
                await release_gate.wait()
                released.set()

        w.engine.release = release  # type: ignore[method-assign]
        await asyncio.wait_for(w.heartbeat_sent.wait(), timeout=5.0)
        scope = w.pool._active_scopes[(ClaimKind.SESSION, "releasing")]
        w.turn_gate["releasing"].set()
        await asyncio.wait_for(release_entered.wait(), timeout=5.0)
        await w.engine.delete_lease(ClaimKind.SESSION, "lost")

        w.heartbeat_gate.set()
        await _until(lambda: w.cancelled["lost"] == 1, "the genuinely lost lease was not preempted")

        release_gate.set()
        await asyncio.wait_for(released.wait(), timeout=5.0)
        w.rearm_gate.set()
        await asyncio.wait_for(w.rearm_done.wait(), timeout=5.0)
        assert scope.cancelled is False
        assert await w.engine.has_lease(ClaimKind.SESSION, "releasing")


@pytest.mark.asyncio
async def test_the_harness_handler_marks_its_scope_before_it_releases():
    """``run_engine_harness`` releases through the pool too: at the moment ``engine.release`` runs the
    execution's scope already carries the mark, and a second in-flight key's scope does not."""
    from primer.model.harness import Harness

    storage = _FakeStorageProvider()
    engine = InMemoryClaimEngine(adapters={})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5),
        scheduler=scheduler, storage=storage,
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = WORKER
    pool._dispatch = {ClaimKind.HARNESS: pool._run_engine_harness}
    # A harness row with no pending operation takes the handler's early-release path.
    await storage.get_storage(Harness).create(
        Harness(id="h-idle", slug="h-idle", name="idle", created_at=datetime.now(timezone.utc)),
    )
    for hid in ("h-idle", "h-other"):
        await engine.upsert(ClaimKind.HARNESS, hid)
    seen: dict[str, bool] = {}
    real_release = engine.release

    async def release(lease, *, outcome):
        scope = pool._active_scopes[(lease.kind, lease.entity_id)]
        seen[lease.entity_id] = scope.lease_returned
        await real_release(lease, outcome=outcome)

    engine.release = release  # type: ignore[method-assign]
    other_gate = asyncio.Event()

    async def other(lease) -> None:
        await other_gate.wait()
        await pool._release_lease(lease, ReleaseOutcome(success=True, drop_lease=True))

    leases = await engine.claim_due(WORKER, max_count=10)
    by_id = {lease.entity_id: lease for lease in leases}
    assert set(by_id) == {"h-idle", "h-other"}
    try:
        tasks = [
            asyncio.create_task(pool._run_engine(by_id["h-idle"], pool._run_engine_harness)),
            asyncio.create_task(pool._run_engine(by_id["h-other"], other)),
        ]
        await asyncio.wait_for(tasks[0], timeout=5.0)
        assert seen == {"h-idle": True}
        other_scope = pool._active_scopes[(ClaimKind.HARNESS, "h-other")]
        assert other_scope.lease_returned is False   # one execution's release marks only its own scope
        other_gate.set()
        await asyncio.wait_for(tasks[1], timeout=5.0)
        assert seen == {"h-idle": True, "h-other": True}
    finally:
        other_gate.set()
        await scheduler.aclose()


def test_no_handler_releases_through_the_engine_directly():
    """Every release of a RUNNING execution goes through ``WorkerPool._release_lease`` (which marks the
    scope first). A syntactic scan, so docstrings and comments cannot trip it: every call named
    ``release`` on something called ``engine`` or ``_engine``, anywhere under ``primer/`` except the engine
    implementations in ``primer/claim/``. The only allowed callers are the helper itself and the give-back
    of a lease that never started (it has no scope and is not in flight)."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "primer"
    found: set[tuple[str, str]] = set()

    class _Scan(ast.NodeVisitor):
        def __init__(self, rel: str) -> None:
            self.rel = rel
            self.stack: list[str] = []

        def _visit_fn(self, node) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = _visit_fn

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "release":
                owner = func.value
                name = owner.id if isinstance(owner, ast.Name) else (
                    owner.attr if isinstance(owner, ast.Attribute) else None
                )
                if name in {"engine", "_engine"}:
                    found.add((self.rel, self.stack[-1] if self.stack else "<module>"))
            self.generic_visit(node)

    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        if rel.startswith("primer/claim/"):
            continue
        _Scan(rel).visit(ast.parse(path.read_text(), filename=rel))
    assert found == {
        ("primer/worker/pool.py", "_release_lease"),
        ("primer/worker/pool.py", "_release_unstarted"),
    }, found


@pytest.mark.asyncio
async def test_the_drain_timeout_still_aborts_a_release_that_hangs():
    """The mark only silences the lost-lease verdict. ``scope.cancel`` stays unconditional, so a drain
    that times out still delivers ``worker_drain_timeout`` to an execution stuck inside its release
    (a second execution, stuck in its turn, gets the same)."""
    engine = InMemoryClaimEngine(adapters={})
    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=4, heartbeat_interval_seconds=1, lease_ttl_seconds=5),
        scheduler=scheduler, storage=None,  # type: ignore[arg-type]
        workspace_registry=None,  # type: ignore[arg-type]
        provider_registry=None,  # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = WORKER
    in_release = asyncio.Event()

    async def hang_release(lease, *, outcome):
        in_release.set()
        await asyncio.Event().wait()

    engine.release = hang_release  # type: ignore[method-assign]

    async def releasing(lease) -> None:
        await pool._release_lease(lease, ReleaseOutcome(success=True, drop_lease=True))

    async def running(lease) -> None:
        await asyncio.Event().wait()

    pool._dispatch = {ClaimKind.HARNESS: releasing, ClaimKind.TRIGGER: running}
    for kind, eid in ((ClaimKind.HARNESS, "stuck-release"), (ClaimKind.TRIGGER, "stuck-turn")):
        await engine.upsert(kind, eid)
    leases = await engine.claim_due(WORKER, max_count=10)
    assert len(leases) == 2
    pool._reserve_and_dispatch(leases)
    await asyncio.wait_for(in_release.wait(), timeout=5.0)
    await _until(lambda: len(pool._active_scopes) == 2, "both executions were not in flight")
    stuck_release = pool._active_scopes[(ClaimKind.HARNESS, "stuck-release")]
    stuck_turn = pool._active_scopes[(ClaimKind.TRIGGER, "stuck-turn")]
    assert stuck_release.lease_returned is True
    assert stuck_turn.lease_returned is False

    await asyncio.wait_for(pool.drain_and_stop(timeout=1.0), timeout=20.0)
    await scheduler.aclose()
    assert stuck_release.cancel_reason == "worker_drain_timeout"
    assert stuck_turn.cancel_reason == "worker_drain_timeout"
