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
import threading
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
    """A BEGIN that raises an ordinary error (not a cancellation) keeps its old behaviour: the error propagates and nothing else
    happens. Pinned as it is, not as a feature: healing a connection left open by an earlier leak (a BEGIN refused with "cannot
    start a transaction within a transaction") is a separate decision, not part of making a cancellation safe."""
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


# ---- review round: a real cancel while BEGIN is queued, and a rollback that itself fails ----------------------------------------


@pytest.mark.parametrize("entry", ["read_snapshot", "transaction"])
async def test_a_real_cancel_while_begin_is_queued_behind_a_busy_worker_thread_leaves_no_open_transaction(
    provider: SqliteStorageProvider, entry: str,
) -> None:
    """No injection: a real task is cancelled while its ``BEGIN`` sits in aiosqlite's queue behind a statement the worker thread is
    still running. The statement cannot be recalled once queued, so ``BEGIN`` runs after the cancellation; the rollback the
    cancellation branch queues runs after it. ``busy()`` blocks the worker on an event so the order is not left to timing."""
    conn = provider.connection
    started, release = threading.Event(), threading.Event()

    def busy() -> int:
        started.set()
        release.wait(15)
        return 1

    await conn.create_function("busy", 0, busy)
    blocker = asyncio.ensure_future(conn.execute("SELECT busy()"))
    assert await asyncio.get_running_loop().run_in_executor(None, started.wait, 15), "the worker never started the busy statement"

    async def unit() -> None:
        async with getattr(provider, entry)():
            pytest.fail("the unit was cancelled before its body")

    task = asyncio.ensure_future(unit())
    await asyncio.sleep(0.05)                 # the task holds the write lock and has queued BEGIN behind busy(); nothing can run it yet
    assert provider._write_lock.locked(), "the unit never reached its BEGIN"
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await blocker

    assert conn.in_transaction is False, f"{entry}: BEGIN ran after the cancellation and nothing rolled it back"
    async with provider.read_snapshot():       # the next request's reads still work
        pass


def _failing_rollback(conn):
    """Make ``conn.rollback`` raise; returns the real one so the test can clean up."""
    real = conn.rollback

    async def broken() -> None:
        raise RuntimeError("rollback failed")

    conn.rollback = broken
    return real


@pytest.mark.parametrize("entry", ["read_snapshot", "transaction"])
async def test_a_failing_rollback_after_a_cancelled_begin_still_raises_the_cancellation(
    provider: SqliteStorageProvider, entry: str,
) -> None:
    """The cancellation must reach the caller whatever the rollback does: a task whose CancelledError is replaced by another error is
    not cancelled any more (the framework that cancelled it sees an ordinary failure)."""
    conn = provider.connection
    real_execute, _ = _cancel_after_the_thread_ran(conn, "BEGIN")
    real_rollback = _failing_rollback(conn)
    try:
        with pytest.raises(asyncio.CancelledError):
            async with getattr(provider, entry)():
                pass
    finally:
        conn.execute = real_execute
        conn.rollback = real_rollback
        await real_rollback()


@pytest.mark.parametrize("entry", ["read_snapshot", "transaction", "_write_guard"])
async def test_a_failing_rollback_never_replaces_a_cancellation_of_the_body(provider: SqliteStorageProvider, entry: str) -> None:
    """The same hazard on the other three unwinding paths: the body is cancelled (a client that disconnects mid-request), the rollback
    that cleans up fails, and the cancellation must still be the exception that propagates."""
    conn = provider.connection
    real_rollback = _failing_rollback(conn)
    try:
        with pytest.raises(asyncio.CancelledError):
            async with getattr(provider, entry)():
                raise asyncio.CancelledError
    finally:
        conn.rollback = real_rollback
        await real_rollback()


@pytest.mark.parametrize("entry", ["read_snapshot", "transaction", "_write_guard"])
async def test_a_failing_rollback_never_replaces_the_error_of_the_body(provider: SqliteStorageProvider, entry: str) -> None:
    conn = provider.connection
    real_rollback = _failing_rollback(conn)
    try:
        with pytest.raises(ValueError, match="the body failed"):
            async with getattr(provider, entry)():
                raise ValueError("the body failed")
    finally:
        conn.rollback = real_rollback
        await real_rollback()


async def test_a_failing_rollback_is_logged_not_swallowed_silently(provider: SqliteStorageProvider, caplog: pytest.LogCaptureFixture) -> None:
    conn = provider.connection
    real_rollback = _failing_rollback(conn)
    try:
        with caplog.at_level("WARNING", logger="primer.storage.sqlite"):
            with pytest.raises(ValueError):
                async with provider.read_snapshot():
                    raise ValueError("the body failed")
    finally:
        conn.rollback = real_rollback
        await real_rollback()

    assert any("rollback failed" in rec.getMessage() for rec in caplog.records), "a rollback that failed must leave a trace"
