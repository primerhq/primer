"""The root guard that fails a test which leaves a ``SqliteStorageProvider`` open (ticket 01a11a8b); see ``tests/_support/sqlite_guard.py``."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from primer.model.except_ import ConfigError
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests._support import sqlite_guard
from tests._support.sqlite_guard import OpenSqliteProviders

REPO_ROOT = Path(__file__).resolve().parents[1]


def _worker_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if "_connection_worker_thread" in t.name]


async def _open_provider(tmp_path: Path, name: str) -> SqliteStorageProvider:
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / f"{name}.sqlite"))
    await provider.initialize()
    return provider


def test_the_root_guard_is_installed_for_every_test() -> None:
    """The autouse fixture in tests/conftest.py is what makes a leak fail its own test; without it this is the stock method."""
    assert SqliteStorageProvider.initialize.__module__ == "tests._support.sqlite_guard"


async def test_a_provider_the_test_closed_is_not_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = OpenSqliteProviders(monkeypatch)
    provider = await _open_provider(tmp_path, "closed")
    await provider.aclose()

    assert guard.close_leaked() == []


async def test_a_provider_left_open_is_reported_where_it_was_opened_and_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = OpenSqliteProviders(monkeypatch)
    provider = await _open_provider(tmp_path, "left-open")

    sites = guard.close_leaked()

    assert len(sites) == 1 and sites[0].startswith("test_sqlite_provider_guard.py:") and sites[0].endswith("(_open_provider)"), sites
    with pytest.raises(ConfigError):
        _ = provider.connection  # closed: a second test cannot be finished off by this one's connection
    assert guard.close_leaked() == [], "a leak is reported once"


async def test_only_the_provider_still_open_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = OpenSqliteProviders(monkeypatch)
    closed = await _open_provider(tmp_path, "one")
    left_open = await _open_provider(tmp_path, "two")
    await closed.aclose()

    assert len(guard.close_leaked()) == 1
    with pytest.raises(ConfigError):
        _ = left_open.connection


async def test_closing_a_leaked_provider_stops_its_worker_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker is a non-daemon thread: one that outlives its test keeps the pytest process from exiting."""
    guard = OpenSqliteProviders(monkeypatch)
    before = set(_worker_threads())
    await _open_provider(tmp_path, "worker")
    started = [t for t in _worker_threads() if t not in before]
    assert len(started) == 1

    guard.close_leaked()

    started[0].join(timeout=10)
    assert not started[0].is_alive()


async def test_a_monkeypatch_undo_in_the_test_does_not_hide_a_close_from_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tests/harness/test_service.py undoes its monkeypatch before it closes its provider; the guard must still see the close (its own MonkeyPatch)."""
    provider = await _open_provider(tmp_path, "undone")

    monkeypatch.undo()
    await provider.aclose()

    assert SqliteStorageProvider.initialize.__module__ == "tests._support.sqlite_guard"
    # a false report would be a teardown error of THIS test, from the autouse guard


async def test_a_repeat_initialize_of_an_open_provider_is_not_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the initialize() that OPENED the connection is the test's to close: a provider a wider-scoped fixture opened earlier must not be closed
    out from under it because a test called initialize() on it again."""
    provider = await _open_provider(tmp_path, "already-open")
    guard = OpenSqliteProviders(monkeypatch)

    await provider.initialize()

    assert guard.close_leaked() == []
    await provider.connection.execute("SELECT 1")     # still open
    await provider.aclose()


async def test_a_close_that_does_not_finish_in_time_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_guard, "_CLOSE_JOIN_S", 0.05)
    guard = OpenSqliteProviders(monkeypatch)
    provider = await _open_provider(tmp_path, "slow-close")
    really_close = provider.aclose

    async def slow_close() -> None:
        await asyncio.sleep(0.5)
        await really_close()

    provider.aclose = slow_close  # type: ignore[method-assign]

    assert len(guard.close_leaked()) == 1
    assert guard.close_finished is False
    for _ in range(100):                              # let the slow close finish so this test itself leaks nothing
        if provider._conn is None:
            break
        await asyncio.sleep(0.05)
    assert provider._conn is None


def test_the_fail_path_names_the_test_and_closes_the_provider(tmp_path: Path) -> None:
    """The fixture in tests/conftest.py, driven the way a real leak drives it: a subprocess pytest run of a test that leaks. The leak makes
    THAT test error at teardown with a message naming it and the initialize() call, and the provider is closed by then (the next test sees it)."""
    inner = tmp_path / "test_inner.py"
    inner.write_text(textwrap.dedent(
        """
        import pytest

        from primer.model.except_ import ConfigError
        from primer.model.provider import SqliteConfig
        from primer.storage.sqlite import SqliteStorageProvider

        LEAKED = []


        async def test_a_leaks(tmp_path):
            provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "x.sqlite"))
            await provider.initialize()
            LEAKED.append(provider)


        def test_b_the_leaked_provider_was_closed():
            with pytest.raises(ConfigError):
                _ = LEAKED[0].connection
        """
    ))
    env = {k: v for k, v in os.environ.items() if k not in {"PYTEST_ADDOPTS", "PRIMER_REQUIRE_POSTGRES_TESTS"}}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", str(inner), "-p", "tests.conftest", "-p", "no:cacheprovider", "-o", "asyncio_mode=auto",
         "--rootdir", str(tmp_path), "-q"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )

    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert "test_inner.py::test_a_leaks left 1 SqliteStorageProvider(s) open (initialize() called at: test_inner.py:" in out, out
    assert "2 passed, 1 error" in out, out           # both test bodies passed; only the leaking test errors, at teardown, and the next one saw it closed
