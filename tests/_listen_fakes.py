"""Fake asyncpg pool + connection for the LISTEN watchers (no database needed).

A LISTEN watcher parks on a queue that asyncpg only feeds from NOTIFY
callbacks. When the server connection dies asyncpg raises into nothing; the one
signal it gives is ``Connection.add_termination_listener``. These fakes model
that contract so the watchers' reaction to a drop can be tested in CI:

* ``FakeListenConn.drop()`` behaves like the server killing the backend: the
  termination callbacks fire once, asyncpg forgets its LISTEN callbacks, and
  every later method call on the (pool-proxied) connection raises
  ``InterfaceError``, as it does once asyncpg has released a dead proxy.
* ``FakeListenPool.release`` on a dropped connection is a no-op, as asyncpg's is.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import asyncpg


class FakeListenConn:
    """Just enough of an asyncpg pool connection for a LISTEN watcher."""

    def __init__(self) -> None:
        self.listeners: dict[str, Callable[..., None]] = {}
        self.termination_listeners: set[Callable[..., None]] = set()
        self.dead = False
        # Every method attempted, in order (including ones that raised).
        self.calls: list[str] = []
        # Method names that raise RuntimeError("boom: <name>") when called.
        self.fail_methods: set[str] = set()

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_methods:
            raise RuntimeError(f"boom: {name}")
        if self.dead:
            raise asyncpg.InterfaceError(
                f"cannot call Connection.{name}(): connection has been "
                "released back to the pool"
            )

    def add_termination_listener(self, callback: Callable[..., None]) -> None:
        self._call("add_termination_listener")
        self.termination_listeners.add(callback)

    def remove_termination_listener(self, callback: Callable[..., None]) -> None:
        self._call("remove_termination_listener")
        self.termination_listeners.discard(callback)

    async def add_listener(self, channel: str, callback: Callable[..., None]) -> None:
        self._call("add_listener")
        self.listeners[channel] = callback

    async def remove_listener(self, channel: str, callback: Callable[..., None]) -> None:
        self._call("remove_listener")
        self.listeners.pop(channel, None)

    def notify(self, channel: str, payload: str) -> None:
        """Deliver a NOTIFY the way asyncpg does: call the LISTEN callback."""
        self.listeners[channel](self, 0, channel, payload)

    def drop(self) -> None:
        """The server connection dies."""
        callbacks = list(self.termination_listeners)
        self.termination_listeners.clear()
        self.listeners.clear()
        self.dead = True
        for cb in callbacks:
            cb(self)


class FakeListenPool:
    """Hands out FakeListenConn objects and records what comes back.

    ``reuse_connection=True`` hands out the SAME connection on every acquire,
    like a hot, LIFO-ordered pooled connection that is checked out and returned
    over and over; anything a user leaves attached to it accumulates.
    """

    def __init__(self, *, reuse_connection: bool = False) -> None:
        self.conns: list[FakeListenConn] = []
        self.released: list[FakeListenConn] = []
        self._acquire_failures: list[BaseException] = []
        self._reuse = reuse_connection

    def fail_next_acquires(self, *excs: BaseException) -> None:
        """The next acquire() calls raise these, in order (a server that is down)."""
        self._acquire_failures.extend(excs)

    async def acquire(self) -> FakeListenConn:
        if self._acquire_failures:
            raise self._acquire_failures.pop(0)
        if self._reuse and self.conns:
            return self.conns[0]
        conn = FakeListenConn()
        self.conns.append(conn)
        return conn

    async def release(self, conn: FakeListenConn) -> None:
        self.released.append(conn)


async def eventually(
    predicate: Callable[[], object], *, timeout: float = 3.0, what: str = "condition",
) -> None:
    """Poll until ``predicate()`` is truthy; fail loudly (not hang) on timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for: {what}")
        await asyncio.sleep(0.01)


async def stop(task: asyncio.Task) -> None:
    """Cancel a consumer task and swallow how it ends (test teardown)."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
