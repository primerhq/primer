"""``close_shielded``: the close a build that did not finish runs on what it left open.

It runs the close on its OWN task (a second cancel of the caller cannot interrupt it), waits for it only up to
``_CLOSE_WAIT_S`` (a silent peer must not stretch the caller's bound), keeps the task referenced until it has finished, and
logs a close that fails instead of raising it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from primer.workspace import base_backend
from primer.workspace.base_backend import close_shielded


class _Closable:
    def __init__(self, *, gate: asyncio.Event | None = None, error: Exception | None = None) -> None:
        self.gate, self.error = gate, error
        self.started = 0
        self.closed = 0

    async def aclose(self) -> None:
        self.started += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        self.closed += 1


async def test_a_close_that_finishes_is_awaited():
    c = _Closable()
    await close_shielded(c, what="x")
    assert c.closed == 1 and not base_backend._PENDING_CLOSES  # noqa: SLF001


async def test_something_without_aclose_is_left_alone():
    await close_shielded(object(), what="x")


async def test_a_close_that_fails_is_logged_and_not_raised(caplog):
    c = _Closable(error=OSError("the socket was already gone"))
    with caplog.at_level(logging.WARNING, logger="primer.workspace"):
        await close_shielded(c, what="the test client")
    assert any("the test client: aclose failed" in r.getMessage() for r in caplog.records)
    assert not base_backend._PENDING_CLOSES  # noqa: SLF001


async def test_a_close_that_never_finishes_does_not_hold_the_caller_past_the_bound(monkeypatch, caplog):
    """A silent peer keeps ``aclose`` waiting for its own timeouts; the caller goes on after ``_CLOSE_WAIT_S`` and the close
    carries on in the background."""
    monkeypatch.setattr(base_backend, "_CLOSE_WAIT_S", 0.2)
    gate = asyncio.Event()
    c = _Closable(gate=gate)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with caplog.at_level(logging.WARNING, logger="primer.workspace"):
        await asyncio.wait_for(close_shielded(c, what="the test client"), timeout=2.0)   # unbounded: fails in seconds
    assert 0.15 < loop.time() - started < 2.0
    assert any("still running after 0.2s" in r.getMessage() for r in caplog.records)
    assert c.closed == 0 and len(base_backend._PENDING_CLOSES) == 1, "still running, and still referenced"  # noqa: SLF001
    gate.set()
    for _ in range(100):
        if c.closed:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    assert c.closed == 1 and not base_backend._PENDING_CLOSES, "it finished on its own and was let go"  # noqa: SLF001


async def test_a_second_cancel_of_the_caller_leaves_the_close_running():
    gate = asyncio.Event()
    c = _Closable(gate=gate)
    task = asyncio.create_task(close_shielded(c, what="x"))
    for _ in range(100):
        if c.started:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.closed == 0 and len(base_backend._PENDING_CLOSES) == 1  # noqa: SLF001
    gate.set()
    for _ in range(100):
        if c.closed:
            break
        await asyncio.sleep(0.01)
    assert c.closed == 1
