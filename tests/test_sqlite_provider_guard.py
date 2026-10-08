"""The root guard that fails a test which leaves a ``SqliteStorageProvider`` open (ticket 01a11a8b); see ``tests/_support/sqlite_guard.py``."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from primer.model.except_ import ConfigError
from primer.model.provider import SqliteConfig
from primer.storage.sqlite import SqliteStorageProvider
from tests._support.sqlite_guard import OpenSqliteProviders


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
