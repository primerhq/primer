"""Tests for the LeaderElector-aware supervisor in _BackgroundTask."""

from __future__ import annotations

import asyncio
import logging

import pytest

from primer.bus.scheduler_tasks import _BackgroundTask
from primer.coordinator.in_memory import InMemoryLeaderElector


class _CountingTask(_BackgroundTask):
    role = "test-role"

    def __init__(self) -> None:
        super().__init__(name="counter")
        self.ticks = 0

    async def _run(self) -> None:
        while not self._stopping:
            self.ticks += 1
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                break


@pytest.mark.asyncio
async def test_supervisor_runs_work_when_elected():
    elector = InMemoryLeaderElector()
    task = _CountingTask()
    task.start(elector)
    await asyncio.sleep(0.05)
    assert task.ticks > 0
    await task.stop()


@pytest.mark.asyncio
async def test_supervisor_stops_cleanly():
    elector = InMemoryLeaderElector()
    task = _CountingTask()
    task.start(elector)
    await asyncio.sleep(0.05)
    await task.stop()
    final = task.ticks
    await asyncio.sleep(0.05)
    assert task.ticks == final


@pytest.mark.asyncio
async def test_legacy_start_without_elector_still_works():
    """Without an elector, start() runs _run unconditionally (legacy path)."""
    task = _CountingTask()
    task.start()  # no elector
    await asyncio.sleep(0.05)
    assert task.ticks > 0
    await task.stop()


class _BoomTask(_BackgroundTask):
    """A work loop that dies on its first tick (its ``_run`` raises):
    the exception must be logged, not lost, and the supervisor must not
    spin acquire/start/fail against the database."""

    role = "test-boom"

    def __init__(self) -> None:
        super().__init__(name="boom")
        self.failures = 0

    async def _run(self) -> None:
        self.failures += 1
        raise RuntimeError("boom: work loop crashed")


class _GatedElector:
    """Counts acquires; the third one blocks on a gate so the test can
    inspect a stable mid-loop state (two booms behind it, none in
    flight)."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.acquires = 0
        self.gate = asyncio.Event()

    async def try_acquire(self, role, **kwargs):
        self.acquires += 1
        if self.acquires == 3:
            await self.gate.wait()
        return await self.inner.try_acquire(role, **kwargs)


@pytest.mark.asyncio
async def test_a_work_loop_that_raises_logs_once_and_backs_off(
    monkeypatch, caplog,
):
    """CORE-13: today a ``_run`` that ends by itself is skipped by the
    supervisor's ``finally`` (it is done), so the exception is only
    found by the GC and the loop re-acquires at once. The fix logs the
    exception once per failure and sleeps ``retry_seconds`` before the
    next acquire."""
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fast_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    task = _BoomTask()
    elector = _GatedElector(InMemoryLeaderElector())
    with caplog.at_level(logging.ERROR, logger="primer.bus.scheduler_tasks"):
        task.start(elector)
        try:
            for _ in range(10000):
                if elector.acquires >= 3:
                    break
                await real_sleep(0.005)
            assert elector.acquires == 3
            # exactly one backoff per re-acquire: the spin is gone
            assert [d for d in sleeps if d == 15.0].count(15.0) == 2
            assert task.failures == 2
            boom_logs = [
                r for r in caplog.records
                if r.levelno == logging.ERROR
                and r.name == "primer.bus.scheduler_tasks"
            ]
            assert len(boom_logs) == 2
            assert all("boom" in r.getMessage() for r in boom_logs)

            elector.gate.set()
            for _ in range(10000):
                if task.failures >= 3:
                    break
                await real_sleep(0.005)
            assert task.failures >= 3  # the loop self-heals and retries
        finally:
            await task.stop()
