"""Tests for matrix.scheduler.postgres.PostgresScheduler.

Real-Postgres tests. They are skipped automatically when the
``PRIMER_PG_TEST_DSN`` environment variable isn't set — no other
test in this repo currently uses a live Postgres, so this file
defines the fixture inline rather than relying on shared infra.

The DSN must be parseable by asyncpg and may include an optional
``?schema=<name>`` query parameter (default: ``public``). The test
uses a unique table name (``workers``) so it won't collide with
application tables in the same schema, but operators are still
encouraged to point at a throwaway DB.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from primer.int.scheduler import CompleteTurnResult, FailureRecord
from primer.model.except_ import ConfigError
from primer.model.provider import PoolConfig, PostgresConfig
from primer.model.scheduler import PostgresSchedulerConfig
from primer.model.workspace_session import WorkspaceSession, SessionStatus
from primer.scheduler.postgres import PostgresScheduler
from primer.storage.postgres import PostgresStorageProvider


_DSN_ENV = "PRIMER_PG_TEST_DSN"

pytestmark = pytest.mark.skipif(
    os.getenv(_DSN_ENV) is None,
    reason=(
        f"set {_DSN_ENV} to a Postgres DSN (e.g. "
        "postgres://user:pw@localhost:5432/matrix_test) to run the "
        "live PostgresScheduler tests"
    ),
)


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
        port=p.port or 5432,
        username=p.username or "postgres",
        password=p.password or "",  # type: ignore[arg-type]
        database=(p.path or "/postgres").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    )


@pytest.fixture
async def storage_provider():
    cfg = _parse_dsn(os.environ[_DSN_ENV])
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


async def test_complete_turn_success(sched, storage_provider):
    """complete_turn advances turn_no and returns SUCCESS."""
    await _insert_session(storage_provider, "s-ct-1")
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    result = await sched.complete_turn(
        "w1", "s-ct-1",
        expected_turn_no=0,
        new_status=SessionStatus.RUNNING,
        re_enqueue=True,
    )
    assert result == CompleteTurnResult.SUCCESS


async def test_complete_turn_conflict_on_wrong_fence(sched, storage_provider):
    await _insert_session(storage_provider, "s-fence-1", turn_no=5)
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    result = await sched.complete_turn(
        "w1", "s-fence-1",
        expected_turn_no=99,
        new_status=SessionStatus.RUNNING,
        re_enqueue=False,
    )
    assert result == CompleteTurnResult.TURN_CONFLICT


async def test_failure_record_writes_columns(sched, storage_provider):
    await _insert_session(storage_provider, "s-fr-1")
    await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4)
    await sched.complete_turn(
        "w1", "s-fr-1",
        expected_turn_no=0,
        new_status=SessionStatus.RUNNING,
        re_enqueue=True,
        record_failure=FailureRecord(error_text="boom", attempt_count=2),
    )
    async with storage_provider.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data FROM sessions WHERE id = $1", "s-fr-1",
        )
    data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
    assert data["attempt_count"] == 2
    assert data["last_error"] == "boom"


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
