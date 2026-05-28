"""Tests for the LeaderElector-aware supervisor in _BackgroundTask."""

from __future__ import annotations

import asyncio

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
