"""Tests for primer.scheduler.postgres.PostgresScheduler.

Real-Postgres tests, gated on ``PRIMER_TEST_POSTGRES_URL`` (the single
gate, see tests/pg_gate.py). The CI Postgres lane runs them with
``PRIMER_REQUIRE_POSTGRES_TESTS=1``, where a skip is a failure.

The DSN must be parseable by asyncpg and may include an optional
``?schema=<name>`` query parameter (default: ``public``). The test
uses a unique table name (``workers``) so it won't collide with
application tables in the same schema, but operators are still
encouraged to point at a throwaway DB.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.scheduler import PostgresSchedulerConfig
from primer.model.workspace_session import WorkspaceSession
from primer.scheduler.postgres import PostgresScheduler
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import CANONICAL_ENV, explicit_port, postgres_marks, require_postgres_url


_DSN_ENV = CANONICAL_ENV

pytestmark = postgres_marks("the live PostgresScheduler tests")


def _parse_dsn(dsn: str) -> PostgresConfig:
    """Translate a DSN into a :class:`PostgresConfig`.

    Recognises ``?schema=<name>`` for the test schema so multiple
    parallel runs can share a database without table collisions.
    """
    p = urlparse(dsn)
    if p.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"unexpected scheme {p.scheme!r} in {_DSN_ENV}")
    query = parse_qs(p.query)
    schema = query.get("schema", ["public"])[0]
    return PostgresConfig(
        hostname=p.hostname or "localhost",
        port=explicit_port(p),
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    )


@pytest.fixture
async def storage_provider():
    cfg = _parse_dsn(require_postgres_url("the live PostgresScheduler tests"))
    sp = PostgresStorageProvider(cfg)
    await sp.initialize()
    # Drop scheduler tables so each test starts clean.
    async with sp.pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS workers")
    try:
        yield sp
    finally:
        async with sp.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS workers")
        await sp.aclose()


@pytest.fixture
async def sched(storage_provider):
    s = PostgresScheduler(
        storage_provider=storage_provider,
        config=PostgresSchedulerConfig(),
    )
    await s.initialize()
    yield s
    await s.aclose()


async def test_initialize_creates_workers_table(sched, storage_provider):
    async with storage_provider.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name = 'workers'"
        )
    names = {r["table_name"] for r in rows}
    assert "workers" in names


async def test_initialize_is_idempotent(sched):
    # Second initialize() must succeed against the already-created
    # tables and re-run the boot-time recovery sweeps without error.
    await sched.initialize()


async def test_register_worker_persists_row(sched):
    await sched.register_worker(
        worker_id="w1", host="h", pid=42, capacity=8,
    )
    workers = await sched.list_workers()
    assert any(w.id == "w1" and w.pid == 42 for w in workers)


async def test_heartbeat_worker_bumps_timestamp(sched):
    import asyncio
    await sched.register_worker(
        worker_id="w1", host="h", pid=1, capacity=1,
    )
    [w_before] = [w for w in await sched.list_workers() if w.id == "w1"]
    await asyncio.sleep(0.05)
    await sched.heartbeat_worker("w1")
    [w_after] = [w for w in await sched.list_workers() if w.id == "w1"]
    assert w_after.last_heartbeat > w_before.last_heartbeat


async def test_drain_worker_changes_status(sched):
    await sched.register_worker(
        worker_id="w1", host="h", pid=1, capacity=1,
    )
    await sched.drain_worker("w1")
    [w] = [w for w in await sched.list_workers() if w.id == "w1"]
    assert w.status == "draining"


async def test_deregister_worker_removes_row(sched):
    await sched.register_worker(
        worker_id="w1", host="h", pid=1, capacity=1,
    )
    await sched.deregister_worker("w1")
    assert all(w.id != "w1" for w in await sched.list_workers())


async def test_register_worker_is_upsert(sched):
    """Re-registering the same worker_id should update the row in place."""
    await sched.register_worker(
        worker_id="w1", host="h1", pid=1, capacity=4,
    )
    await sched.register_worker(
        worker_id="w1", host="h2", pid=2, capacity=8,
    )
    workers = [w for w in await sched.list_workers() if w.id == "w1"]
    assert len(workers) == 1
    assert workers[0].host == "h2"
    assert workers[0].pid == 2
    assert workers[0].capacity == 8


async def _insert_session(storage_provider, sid: str, *,
                          turn_no: int = 0, status: str = "running"):
    """Force-create the sessions table and insert a synthetic row.
    Bypasses Storage[WorkspaceSession] to keep these tests focused on scheduler
    behaviour."""
    sp_storage = storage_provider.get_storage(WorkspaceSession)
    await sp_storage._ensure_table()  # noqa: SLF001
    async with storage_provider.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (id, data, created_at, updated_at)
            VALUES ($1, $2::jsonb, now(), now())
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """,
            sid,
            json.dumps({
                "workspace_id": "ws-1",
                "binding": {"kind": "agent", "agent_id": "ag-1"},
                "status": status,
                "turn_no": turn_no,
                "attempt_count": 0,
                "metadata": {},
                "pause_requested": False,
                "cancel_requested": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }),
        )


async def test_enqueue_sends_notify(sched, storage_provider):
    """enqueue() sends pg_notify; watch_ready picks it up."""
    await _insert_session(storage_provider, "s-watch-1")
    await sched.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    iterator = sched.watch_ready("w1")

    async def consume():
        async for sid in iterator:
            return sid

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)  # let LISTEN attach
    await sched.enqueue("s-watch-1")
    sid = await asyncio.wait_for(task, timeout=2.0)
    assert sid == "s-watch-1"


async def test_signal_cancel_yields_to_watcher(sched):
    await sched.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    # _watch_cancel is a parallel test seam (not on the ABC) — same shape as watch_ready
    iterator = sched._watch_cancel("w1")

    async def consume():
        async for sid in iterator:
            return sid

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    await sched.signal_cancel("s-cancel-1")
    sid = await asyncio.wait_for(task, timeout=2.0)
    assert sid == "s-cancel-1"


# ---------------------------------------------------------------------------
# LISTEN watcher: the pooled connection must always go back
# ---------------------------------------------------------------------------
# A watcher holds one pooled connection for its LISTEN. asyncpg's Pool.close()
# waits for every acquired connection to be released, with no timeout, so a
# leaked one hangs shutdown forever. It used to be released only on
# CancelledError / Exception, not when the consumer stops iterating
# (GeneratorExit), and PostgresScheduler.aclose() could not release it because
# it tracked nothing. These tests build their own provider (not the `sched`
# fixture, whose teardown would hang the whole suite on a regression) and
# bound every pool close.


async def _fresh_pair(**scheduler_config):
    sp = PostgresStorageProvider(_parse_dsn(require_postgres_url()))
    await sp.initialize()
    async with sp.pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS workers")
    s = PostgresScheduler(
        storage_provider=sp, config=PostgresSchedulerConfig(**scheduler_config),
    )
    await s.initialize()
    return sp, s


def _held(sp) -> int:
    """Connections currently acquired from the pool (leaked LISTEN conns)."""
    return sp.pool.get_size() - sp.pool.get_idle_size()


async def _close_pool_bounded(sp) -> None:
    held = _held(sp)
    try:
        await asyncio.wait_for(sp.aclose(), 5.0)
    except TimeoutError:
        sp.pool.terminate()
        pytest.fail(
            f"pool.close() hung: a LISTEN connection was never released ({held} still held)"
        )


async def _consume_one_then_stop(sched, iterator) -> str:
    """Consume exactly one item and return, abandoning the iterator."""

    async def consume():
        async for sid in iterator:
            return sid

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)  # let LISTEN attach
    await sched.signal_cancel("s-leak")
    return await asyncio.wait_for(task, timeout=3.0)


async def test_explicitly_closed_watcher_releases_its_listen_connection():
    sp, sched = await _fresh_pair()
    try:
        it = sched._watch_cancel("w1")
        assert await _consume_one_then_stop(sched, it) == "s-leak"
        assert _held(sp) == 1  # the abandoned watcher still holds its LISTEN conn
        await it.aclose()
        assert _held(sp) == 0, "closing the generator must release its connection"
    finally:
        await _close_pool_bounded(sp)


async def test_collected_watcher_releases_its_listen_connection():
    """The production shape: the consumer returns, nothing references the
    generator, and asyncio's finalizer closes it, so its `finally` must run
    GeneratorExit and release the connection. This is the real bug fix (1)
    closes; on main the finally was missing and this leaked and hung."""
    import gc

    sp, sched = await _fresh_pair()
    try:
        it = sched._watch_cancel("w1")
        assert await _consume_one_then_stop(sched, it) == "s-leak"
        del it
        gc.collect()
        await asyncio.sleep(0.3)  # let the asyncgen finalizer's aclose() run
        assert _held(sp) == 0, "a garbage-collected watcher must release its connection"
    finally:
        await _close_pool_bounded(sp)


async def test_scheduler_aclose_releases_an_abandoned_watcher():
    """An abandoned watcher that something still REFERENCES.

    When a consumer returns from `async for`, the suspended generator is
    normally closed by asyncio's async-generator finalizer on the next loop
    iteration once nothing references it (test_collected_watcher_releases_...
    pins that; it is the production shape, and fix (1) alone covers it). If
    something DOES keep a reference, that finalizer never fires, the
    connection stays acquired and Pool.close() waits forever.
    PostgresScheduler.aclose() is defence in depth for that case: the iterator
    is deliberately kept alive here, with no close and no gc, so only aclose()
    can rescue the connection."""
    sp, sched = await _fresh_pair()
    try:
        it = sched._watch_cancel("w1")
        assert await _consume_one_then_stop(sched, it) == "s-leak"
        assert _held(sp) == 1
        await sched.aclose()
        assert _held(sp) == 0, "PostgresScheduler.aclose() must release live listeners"
        # The abandoned generator is closed later; its release must be a no-op.
        await it.aclose()
        assert _held(sp) == 0
    finally:
        await _close_pool_bounded(sp)


async def test_cancelled_watcher_releases_its_listen_connection():
    """Control: cancelling the consumer while parked in the generator
    (the pool's normal shutdown path) always released correctly."""
    sp, sched = await _fresh_pair()
    try:
        it = sched._watch_cancel("w1")

        async def consume():
            async for _ in it:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.3)
        assert _held(sp) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _held(sp) == 0
    finally:
        await _close_pool_bounded(sp)


# ---------------------------------------------------------------------------
# LISTEN watcher: the server connection dying must be noticed
# ---------------------------------------------------------------------------
# asyncpg never raises into the NOTIFY queue when the backend goes away (a
# Postgres restart or failover, a network blip). Without a termination
# listener the watcher parks forever and every later user cancel is silently
# never delivered. The no-DB twin is test_postgres_listen_drop.py.


async def _eventually(predicate, *, timeout: float = 10.0, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"timed out after {timeout}s waiting for: {what}")
        await asyncio.sleep(0.02)


async def test_watcher_resubscribes_after_its_listen_backend_is_terminated():
    sp, sched = await _fresh_pair(listen_reconnect_seconds=0.1)
    got: list[str] = []
    it = sched._watch_cancel("w1")

    async def consume():
        async for sid in it:
            got.append(sid)

    task = asyncio.create_task(consume())
    try:
        await _eventually(lambda: len(sched._listeners) == 1, what="the first LISTEN")
        (first,) = sched._listeners
        old_pid = first.conn.get_server_pid()
        await sched.signal_cancel("before-drop")
        await _eventually(lambda: got == ["before-drop"], what="the pre-drop cancel")

        async with sp.pool.acquire() as conn:
            assert await conn.fetchval("SELECT pg_terminate_backend($1)", old_pid)

        await _eventually(
            lambda: sched._listeners and first not in sched._listeners,
            what="a replacement LISTEN connection",
        )
        (second,) = sched._listeners
        assert second.conn.get_server_pid() != old_pid
        await sched.signal_cancel("after-drop")
        await _eventually(lambda: got == ["before-drop", "after-drop"], what="the post-drop cancel")
        assert sched._listen_reconnects_total == 1
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await _close_pool_bounded(sp)


async def test_initialize_adds_a_nullable_load_column_to_the_workers_table(sched, storage_provider):
    """Lead sweep M2. The table is created without the column (the fixture drops it first, as a deployment that predates load reporting
    has it) and initialize adds it: nullable and with no default, so a worker that never reports reads NULL, not 0."""
    async with storage_provider.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'workers' AND column_name = 'in_flight' AND table_schema = current_schema()"
        )
    assert row is not None, "initialize did not add workers.in_flight"
    assert (row["data_type"], row["is_nullable"], row["column_default"]) == ("integer", "YES", None)


async def test_report_worker_load_is_stored_and_a_new_registration_forgets_it(sched):
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    [fresh] = [w for w in await sched.list_workers() if w.id == "w1"]
    assert fresh.in_flight is None, "a worker that never reported must read as unknown"

    await sched.report_worker_load("w1", in_flight=3)
    [reported] = [w for w in await sched.list_workers() if w.id == "w1"]
    assert reported.in_flight == 3

    await sched.register_worker(worker_id="w1", host="h", pid=2, capacity=4)
    [again] = [w for w in await sched.list_workers() if w.id == "w1"]
    assert again.in_flight is None, "the previous process's load is not the restarted worker's"


async def test_report_worker_load_does_not_touch_the_heartbeat(sched):
    import asyncio

    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=1)
    [before] = [w for w in await sched.list_workers() if w.id == "w1"]
    await asyncio.sleep(0.05)

    await sched.report_worker_load("w1", in_flight=1)

    [after] = [w for w in await sched.list_workers() if w.id == "w1"]
    assert after.last_heartbeat == before.last_heartbeat, "load is reported beside the heartbeat, it is not a heartbeat"


async def test_initialize_does_not_lock_the_workers_table_once_it_has_the_load_column(sched, storage_provider):
    """#484 review. ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` takes an ACCESS EXCLUSIVE lock before it finds there is nothing to do, so
    running it on every boot would queue behind (and then block) every heartbeat and health read. With the column already there,
    initialize must not reach for that lock: it completes while another connection holds the table open."""
    import asyncio

    async with storage_provider.pool.acquire() as holder:
        async with holder.transaction():
            await holder.fetch("SELECT * FROM workers")  # an ACCESS SHARE lock, held until the transaction ends
            await asyncio.wait_for(sched.initialize(), timeout=3.0)
