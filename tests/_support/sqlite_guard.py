"""Fail the test that leaves a ``SqliteStorageProvider`` open (ticket 01a11a8b), instead of letting some later test pay for it.

An aiosqlite connection owns a worker thread. A provider a test never ``aclose()``d is finished by the garbage collector, at an arbitrary
moment, inside whichever test happens to be running: ``Connection.__del__`` asks the worker to stop through a future created on the loop that is
current THEN, and the worker thread delivers the answer with ``future.get_loop().call_soon_threadsafe``. When that loop has been closed the worker
dies with ``RuntimeError: Event loop is closed`` and pytest reports a ``PytestUnhandledThreadExceptionWarning`` against the innocent test. A
connection that is still referenced at exit is worse: its worker is a non-daemon thread, so the interpreter never finishes and the pytest process
hangs after its last test.

:class:`OpenSqliteProviders` records every provider a test initialises and forgets it when it is closed. :meth:`close_leaked` closes the survivors on
a private event loop (in its own thread, so the test loop and the loop policy are untouched) and returns where each was opened, for the failure
message. Wired as an autouse fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
_CLOSE_JOIN_S = 30.0


def _opened_at(test_file: Path | None) -> str:
    """The first frame under ``tests/`` (or in the running test's own file) that is awaiting the ``initialize`` being tracked: the test, or the helper it built the provider in."""
    frame = sys._getframe(2)
    while frame is not None:
        filename = frame.f_code.co_filename
        path = Path(filename).resolve()
        if path != _THIS_FILE and (f"{os.sep}tests{os.sep}" in filename or path == test_file):
            return f"{path.name}:{frame.f_lineno} ({frame.f_code.co_name})"
        frame = frame.f_back
    return "a frame outside tests/"


class OpenSqliteProviders:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, test_file: Path | None = None) -> None:
        from primer.storage.sqlite import SqliteStorageProvider

        self._open: dict[int, tuple[SqliteStorageProvider, str]] = {}
        self.close_finished = True
        test_file = Path(test_file).resolve() if test_file is not None else None
        real_initialize = SqliteStorageProvider.initialize
        real_aclose = SqliteStorageProvider.aclose
        open_ = self._open

        async def initialize(provider: SqliteStorageProvider) -> None:
            # Only an initialize() that OPENED the connection is this test's to close: a repeat call on a provider that was already open (an
            # idempotent re-initialize, or one a wider-scoped fixture opened before the test) opens nothing.
            was_closed = provider._conn is None
            await real_initialize(provider)
            if was_closed:
                open_.setdefault(id(provider), (provider, _opened_at(test_file)))

        async def aclose(provider: SqliteStorageProvider) -> None:
            try:
                await real_aclose(provider)
            finally:
                open_.pop(id(provider), None)

        monkeypatch.setattr(SqliteStorageProvider, "initialize", initialize)
        monkeypatch.setattr(SqliteStorageProvider, "aclose", aclose)

    def close_leaked(self) -> list[str]:
        """Close every provider still open and return where each was opened (empty when the test closed its own).

        ``close_finished`` says whether the close completed within the join bound; when it did not, the worker thread may still be running.
        """
        leaked = list(self._open.values())
        self._open.clear()
        self.close_finished = True
        if leaked:
            self.close_finished = _close_on_a_private_loop([provider for provider, _ in leaked])
        return [site for _, site in leaked]


def _close_on_a_private_loop(providers) -> bool:
    """Close ``providers`` on a loop of their own; True when that finished within ``_CLOSE_JOIN_S``."""

    async def close_all() -> None:
        await asyncio.gather(*(provider.aclose() for provider in providers), return_exceptions=True)

    closer = threading.Thread(target=lambda: asyncio.run(close_all()), name="close-leaked-sqlite-providers", daemon=True)
    closer.start()
    closer.join(_CLOSE_JOIN_S)
    return not closer.is_alive()
