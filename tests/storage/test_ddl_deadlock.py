"""Live repro of the lane N round 3 deadlock (ruling N3-1): a plain idempotent
``CREATE INDEX IF NOT EXISTS`` racing ``CREATE INDEX CONCURRENTLY`` on the
same table.

Lane N's background builder (``PostgresModelStorage._maintain_indexes``)
builds missing non-unique hot indexes CONCURRENTLY. The CIC build holds
SHARE UPDATE EXCLUSIVE on the table for the session and, in its final wait
(Postgres ``WaitForOlderSnapshots``), blocks on every older virtual
transaction that still advertises a snapshot. A plain DDL whose transaction
started earlier (and so is in that wait) but whose table lock request then
queues behind the CIC's SHARE UPDATE EXCLUSIVE forms a two-node deadlock
cycle; the detector kills one side with DeadlockDetectedError. The ensure
side must retry its idempotent DDL unit in a fresh transaction
(``execute_idempotent_ddl``); the CIC side self-retries in
``_maintain_indexes`` and is left as is.

The choreography below makes the deadlock deterministic (verified on
Postgres 16; the plain side is the consistent victim):
  1. the plain transaction starts and runs a filler statement, so the
     backend advertises an xmin (an idle-in-transaction backend with no
     snapshot is skipped by the CIC wait);
  2. the CIC starts: its SHARE UPDATE EXCLUSIVE is granted (the filler holds
     no table lock), it builds, and its final wait blocks on the plain's
     virtual transaction (wait event ``virtualxid``);
  3. the filler ends; the plain DDL now runs in the same transaction: its
     ShareLock request queues behind the CIC's SHARE UPDATE EXCLUSIVE;
  4. two-node cycle: the CIC waits on the plain's virtual transaction and
     the plain waits on the CIC's table lock -> the detector fires in ~1s.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import asyncpg
import pytest
import pytest_asyncio

from primer.storage._ddl import execute_idempotent_ddl
from tests.pg_gate import CANONICAL_ENV, explicit_port, needs_postgres, require_postgres_url

_URL_ENV = CANONICAL_ENV
# The filler keeps the plain xact advertising an xmin while the CIC reaches
# its final wait; it must outlive that point and end before the DDL.
FILLER_SECONDS = 4.0

_needs_pg = needs_postgres("DDL deadlock retry tests")


def _dsn_from(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in {"postgres", "postgresql"}:
        raise AssertionError(f"unexpected scheme {p.scheme!r} in {_URL_ENV}")
    db = (p.path or "/postgres").lstrip("/") or "postgres"
    return f"postgresql://{p.username}:{p.password}@{p.hostname}:{explicit_port(p)}/{db}"


@pytest_asyncio.fixture
async def pg_pool() -> AsyncIterator[asyncpg.Pool]:
    """A dedicated throwaway pool whose connections the test choreographs."""
    url = require_postgres_url("DDL deadlock retry tests")
    pool = await asyncpg.create_pool(dsn=_dsn_from(url), min_size=1, max_size=8)
    try:
        yield pool
    finally:
        await pool.close()


async def _wait_for(pool: asyncpg.Pool, sql: str, args: tuple, what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if await pool.fetchval(sql, *args):
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for the {what} choreography phase")
        await asyncio.sleep(0.05)


@_needs_pg
@pytest.mark.asyncio
async def test_ensure_ddl_retries_past_a_deadlock_with_a_concurrent_build(pg_pool: asyncpg.Pool) -> None:
    """The plain DDL unit is aborted by the deadlock detector; the helper re-runs
    it in a fresh transaction (by then the CIC has finished and released its
    lock) and the index must exist and be valid."""
    table = f"ddl_deadlock_{uuid.uuid4().hex[:10]}"
    plain_ddl = f"CREATE INDEX IF NOT EXISTS ddl_deadlock_plain ON {table} (id)"
    cic_ddl = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ddl_deadlock_cic ON {table} (id)"
    conns: list[asyncpg.Connection] = []
    tasks: list[asyncio.Task] = []
    state: dict[str, object] = {"cic_task": None}
    creator = await pg_pool.acquire()
    conns.append(creator)
    await creator.execute(f"CREATE TABLE {table} (id text PRIMARY KEY, data jsonb)")
    try:
        plain = await pg_pool.acquire()
        conns.append(plain)
        cic = await pg_pool.acquire()
        conns.append(cic)
        cic_pid = await cic.fetchval("SELECT pg_backend_pid()")

        async def _plain_unit() -> None:
            # The unit is the idempotent DDL in its own transaction (the
            # _ensure_table_once shape); attempt 1 also runs the choreography
            # gap so the CIC's final wait blocks on this transaction.
            async with plain.transaction():
                if state["cic_task"] is None:
                    filler = asyncio.create_task(plain.execute(f"SELECT pg_sleep({FILLER_SECONDS})"))
                    tasks.append(filler)
                    state["cic_task"] = asyncio.create_task(cic.execute(cic_ddl))
                    tasks.append(state["cic_task"])
                    await _wait_for(
                        pg_pool,
                        "SELECT count(*) FROM pg_stat_activity WHERE pid=$1 AND state='active' "
                        "AND wait_event_type='Lock' AND wait_event='virtualxid'",
                        (cic_pid,),
                        "the CIC to wait on the plain's virtual transaction",
                    )
                    await filler  # the xact no longer advertises an xmin; the CIC stays blocked on it
                await plain.execute(plain_ddl)  # attempt 1: queues behind the CIC -> deadlock

        await execute_idempotent_ddl(_plain_unit)

        # The CIC either finished (the plain was the victim and its xact died)
        # or deadlocked itself (the background builder would retry it there;
        # record and suppress it here).
        cic_outcome = "done"
        cic_task = state["cic_task"]
        assert cic_task is not None
        try:
            await asyncio.wait_for(cic_task, timeout=30)
        except TimeoutError:
            cic_outcome = "timeout"
        except asyncpg.DeadlockDetectedError:
            cic_outcome = "deadlock"
        assert cic_outcome in {"done", "deadlock"}, f"the CIC build ended badly: {cic_outcome}"

        assert await creator.fetchval(
            "SELECT indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE c.relname=$1",
            "ddl_deadlock_plain",
        ), "the plain index must exist and be valid after the deadlock"
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t
        for c in conns:
            if c is creator:
                continue
            with contextlib.suppress(Exception):
                await c.close()
        with contextlib.suppress(Exception):
            await creator.execute(f"DROP TABLE IF EXISTS {table}")
        await pg_pool.release(creator)
