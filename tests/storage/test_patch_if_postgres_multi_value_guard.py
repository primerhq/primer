"""A multi-valued ``patch_if`` guard is re-checked against the row a concurrent writer committed, on a live Postgres
(ticket 01a1247f-a1b3).

Writer A moves the row inside an open transaction, so it holds the row lock; writer B's ``patch_if`` (through
``patch_if_checked``, the drift tripwire) blocks on that lock; A commits. Under READ COMMITTED, Postgres then re-checks B's
WHERE against A's committed version (EvalPlanQual) and applies or refuses B on THAT version. A ``where`` list compiled as
``(data -> f) IN (SELECT jsonb_array_elements($n))`` is planned as a semi-join, and the re-check reuses the one list element
the first pass matched instead of the whole list: a row A moved to ANOTHER allowed value was refused, and
``patch_if_checked`` then logged an ERROR and counted ``storage_cas_drift_total`` (the signal for a guard that can never
apply) for a write that should simply have applied. The in-memory fake and SQLite have no such re-check, so this runs on
Postgres only (gate: ``PRIMER_TEST_POSTGRES_URL``, see ``tests/pg_gate.py``), with two real connections and a real row lock.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import pytest
import pytest_asyncio

import primer.observability.metrics as metrics
from primer.model.provider import PoolConfig, PostgresConfig
from primer.storage import postgres as pg_backend
from primer.storage.cas import patch_if_checked
from primer.storage.postgres import PostgresStorageProvider
from tests.pg_gate import explicit_port, postgres_marks, require_postgres_url
from tests.storage._patch_scenarios import PatchDoc

_WHAT = "the Postgres patch_if guard re-check tests"
pytestmark = [*postgres_marks(_WHAT), pytest.mark.asyncio]

ROW = "guarded"
_BEFORE = {"status": "waiting", "token": "t1", "count": 7}
#: Every wait is bounded: a writer that never unblocks fails the test instead of hanging the lane.
_BOUND_S = 30.0


@pytest_asyncio.fixture
async def provider() -> AsyncIterator[PostgresStorageProvider]:
    """A provider on a throwaway schema of the gated database, dropped afterwards."""
    url = urlparse(require_postgres_url(_WHAT))
    schema = f"mvg{uuid.uuid4().hex[:16]}"
    sp = PostgresStorageProvider(PostgresConfig(
        hostname=url.hostname or "localhost",
        port=explicit_port(url),
        username=url.username or "primer",
        password=url.password or "",  # type: ignore[arg-type]
        database=(url.path or "/").lstrip("/") or "postgres",
        db_schema=schema,
        pool=PoolConfig(min_size=1, max_size=4),
    ))
    async with asyncio.timeout(_BOUND_S):
        await sp.initialize()
    try:
        yield sp
    finally:
        async with asyncio.timeout(_BOUND_S):
            async with sp.pool.acquire() as c:
                await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            # The "table ensured" cache is keyed by id(provider): a later provider at this address must not skip its CREATE.
            for key in [k for k in pg_backend._table_ensured if k[0] == id(sp)]:
                pg_backend._table_ensured.discard(key)
            await sp.aclose()


def _drift() -> float:
    return metrics.storage_cas_drift_total.labels("PatchDoc")._value.get()


async def _until_waiting_on(provider: PostgresStorageProvider, blocker: Any, waiter: Any, task: asyncio.Task[Any]) -> None:
    """Return once ``waiter``'s backend waits on a lock ``blocker``'s backend holds. Fails if ``task`` ends first: B then never
    waited, and the re-check this file is about never ran. The caller bounds the wait."""
    blocker_pid, waiter_pid = blocker.get_server_pid(), waiter.get_server_pid()
    async with provider.pool.acquire() as probe:
        while not task.done():
            if await probe.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = $2 AND $1 = ANY(pg_blocking_pids(pid))",
                blocker_pid, waiter_pid,
            ):
                return
            await asyncio.sleep(0.02)
    raise AssertionError("B finished without waiting on A's row lock, so the re-check was never exercised")


async def _b_blocked_on_a(
    provider: PostgresStorageProvider, a_patch: Mapping[str, Any], b_where: Mapping[str, Sequence[Any]],
) -> Any:
    """A writes ``a_patch`` and holds the row lock; B's ``patch_if_checked({"flag": True}, where=b_where)`` blocks on it; A
    commits; B's result is returned. On any failure A is rolled back (which frees B) and B is awaited. Every await is
    bounded, the connections' acquire and release included."""
    store = provider.get_storage(PatchDoc)
    async with asyncio.timeout(3 * _BOUND_S), provider.pool.acquire() as c1, provider.pool.acquire() as c2:
        tx1 = c1.transaction()
        await tx1.start()
        committed = False
        b_task: asyncio.Task[Any] | None = None
        try:
            async with asyncio.timeout(_BOUND_S):
                assert await store.patch_if(ROW, a_patch, where={"status": ["waiting"]}, conn=c1) is not None
                b_task = asyncio.create_task(patch_if_checked(store, ROW, {"flag": True}, where=b_where, conn=c2))
                await _until_waiting_on(provider, c1, c2, b_task)
                await tx1.commit()
                committed = True
                return await b_task
        finally:
            async with asyncio.timeout(_BOUND_S):
                if not committed:
                    await tx1.rollback()
                if b_task is not None:
                    await asyncio.gather(b_task, return_exceptions=True)


_CASES = [
    # (a) A moved the guarded field from one value B allows to ANOTHER value B allows: B applies.
    pytest.param({"status": "paused"}, {"status": ["waiting", "paused"]}, True, id="multi-moved-to-another-allowed-value"),
    pytest.param(
        {"status": "paused", "token": "t2"}, {"status": ["waiting", "paused"], "token": ["t1", "t2"]}, True,
        id="two-multi-terms-both-moved-within-their-sets",
    ),
    pytest.param({"token": None}, {"token": ["t1", None]}, True, id="multi-with-none-moved-to-null"),
    # (b) A moved it OUT of B's set: a genuine refusal, which the tripwire must not count as drift.
    pytest.param({"status": "ended"}, {"status": ["waiting", "paused"]}, False, id="multi-moved-out-of-the-set"),
    # (c) A wrote an unrelated field only: B applies.
    pytest.param({"count": 8}, {"status": ["waiting", "paused"]}, True, id="multi-unrelated-field-only"),
    # Controls: one allowed value.
    pytest.param({"status": "paused"}, {"status": ["waiting"]}, False, id="single-moved-away"),
    pytest.param({"count": 8}, {"status": ["waiting"]}, True, id="single-unrelated-field-only"),
]


@pytest.mark.parametrize("a_patch,b_where,applies", _CASES)
async def test_a_blocked_patch_if_is_rechecked_against_the_version_the_concurrent_writer_committed(
    provider: PostgresStorageProvider,
    caplog: pytest.LogCaptureFixture,
    a_patch: dict[str, Any],
    b_where: dict[str, list[Any]],
    applies: bool,
) -> None:
    store = provider.get_storage(PatchDoc)
    async with asyncio.timeout(_BOUND_S):
        await store.create(PatchDoc(id=ROW, **_BEFORE))
    drift_before = _drift()
    with caplog.at_level(logging.ERROR, logger="primer.storage.cas"):
        out = await _b_blocked_on_a(provider, a_patch, b_where)
    cas_errors = [r.getMessage() for r in caplog.records if r.name == "primer.storage.cas"]

    assert {"applied": out is not None, "drift": _drift() - drift_before, "cas_errors": cas_errors} == {
        "applied": applies, "drift": 0, "cas_errors": [],
    }, f"B guarded on {b_where} after A committed {a_patch}"
    async with asyncio.timeout(_BOUND_S):
        after = await store.get(ROW)
    assert after is not None
    committed_by_a = {**_BEFORE, **a_patch}
    assert {k: getattr(after, k) for k in committed_by_a} == committed_by_a, "A's committed write survives"
    assert after.flag is applies, "B's own write landed exactly when it applied"
