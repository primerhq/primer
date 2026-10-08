"""A cancellation that lands right after ``BEGIN`` must not leave the shared SQLite connection inside a transaction.

``SqliteStorageProvider`` serves every request from ONE aiosqlite connection. ``read_snapshot()`` and ``transaction()`` issue
``BEGIN`` and then enter a ``try`` whose ``except BaseException`` rolls back, but the ``await conn.execute("BEGIN")`` itself was
OUTSIDE that ``try``. aiosqlite runs the statement on a worker thread, so a task cancelled while it awaits can be cancelled AFTER the
thread already executed ``BEGIN``: the task unwinds, the write lock is released, and the connection stays in a transaction that
nothing tracks. Every later ``BEGIN`` (any ``list``, any ``read_snapshot``) fails with "cannot start a transaction within a
transaction" until the process restarts.

How it shows up: a browser that leaves a page while its request is in flight (the Platform > Toolsets page loads ``GET /v1/tools``, a
fan-out of snapshot reads, and cancels it when you navigate away; a closed tab does the same). The server answers 502 to everything
afterwards. Found while driving the console with Playwright on a SQLite scratch instance: it wedged after one run of two page loads.

The race is made deterministic here: the wrapped ``execute`` runs ``BEGIN`` for real, then raises ``CancelledError``, which is exactly
what the awaiting task sees when the cancellation arrives after the thread has run the statement.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def provider(tmp_path: Path):
    p = SqliteStorageProvider(SqliteConfig(path=tmp_path / "cancelled_begin.sqlite"))
    await p.initialize()
    try:
        yield p
    finally:
        await p.aclose()


def _cancel_after_the_thread_ran(conn, sql_to_cancel: str):
    """Replace ``conn.execute`` with a version that runs the statement and then raises ``CancelledError`` for ``sql_to_cancel`` only."""
    real = conn.execute
    cancelled = {"count": 0}

    async def execute(sql, *args, **kwargs):
        cursor = await real(sql, *args, **kwargs)
        if isinstance(sql, str) and sql.strip().upper() == sql_to_cancel and cancelled["count"] == 0:
            cancelled["count"] += 1
            raise asyncio.CancelledError
        return cursor

    conn.execute = execute
    return real, cancelled


@pytest.mark.parametrize("entry", ["read_snapshot", "transaction"])
async def test_a_cancel_right_after_begin_leaves_no_open_transaction(provider: SqliteStorageProvider, entry: str) -> None:
    conn = provider.connection
    real, cancelled = _cancel_after_the_thread_ran(conn, "BEGIN")
    try:
        with pytest.raises(asyncio.CancelledError):
            async with getattr(provider, entry)():
                pytest.fail("the body must not run: the entry was cancelled")
    finally:
        conn.execute = real

    assert cancelled["count"] == 1, "the cancellation was not injected"
    assert conn.in_transaction is False, f"{entry}: a cancelled BEGIN left the shared connection inside a transaction"


@pytest.mark.parametrize("entry", ["read_snapshot", "transaction"])
async def test_the_provider_keeps_working_after_a_cancelled_begin(provider: SqliteStorageProvider, entry: str) -> None:
    """The user-visible symptom: the next reader, a different request, fails with 'cannot start a transaction within a transaction'."""
    conn = provider.connection
    real, _ = _cancel_after_the_thread_ran(conn, "BEGIN")
    try:
        with pytest.raises(asyncio.CancelledError):
            async with getattr(provider, entry)():
                pass
    finally:
        conn.execute = real

    async with provider.read_snapshot():                       # a later request's reads
        pass
    async with provider.transaction():                         # and a later request's multi-write unit
        pass
    assert conn.in_transaction is False


async def test_a_begin_that_fails_on_its_own_still_raises_and_is_not_rolled_back_for_it(provider: SqliteStorageProvider) -> None:
    """A BEGIN that raises an ordinary error (not a cancellation) keeps its old behaviour: the error propagates and nothing is rolled
    back on its behalf, so a write another coroutine has in flight on the shared connection is not discarded."""
    conn = provider.connection
    real = conn.execute
    rolled_back: list[bool] = []
    real_rollback = conn.rollback

    async def execute(sql, *args, **kwargs):
        if isinstance(sql, str) and sql.strip().upper() == "BEGIN":
            raise RuntimeError("BEGIN refused")
        return await real(sql, *args, **kwargs)

    async def rollback():
        rolled_back.append(True)
        return await real_rollback()

    conn.execute = execute
    conn.rollback = rollback
    try:
        with pytest.raises(RuntimeError, match="BEGIN refused"):
            async with provider.read_snapshot():
                pass
        with pytest.raises(RuntimeError, match="BEGIN refused"):
            async with provider.transaction():
                pass
    finally:
        conn.execute = real
        conn.rollback = real_rollback

    assert rolled_back == [], "a BEGIN that failed on its own must not trigger a rollback"


async def test_a_normal_snapshot_and_transaction_still_work_and_release_the_lock(provider: SqliteStorageProvider) -> None:
    async with provider.read_snapshot():
        assert provider.connection.in_transaction is True
    assert provider.connection.in_transaction is False
    async with provider.transaction():
        assert provider.connection.in_transaction is True
    assert provider.connection.in_transaction is False
    assert not provider._write_lock.locked(), "the write lock must be free after the units"
