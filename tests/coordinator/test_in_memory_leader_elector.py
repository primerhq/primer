"""Tests for primer.coordinator.in_memory.InMemoryLeaderElector."""

from __future__ import annotations

import asyncio

import pytest

from primer.coordinator.in_memory import InMemoryLeaderElector


@pytest.mark.asyncio
async def test_try_acquire_always_succeeds():
    elector = InMemoryLeaderElector()
    lease = await elector.try_acquire("role-a")
    assert lease is not None
    assert lease.role == "role-a"


@pytest.mark.asyncio
async def test_lease_lost_event_does_not_fire_spontaneously():
    elector = InMemoryLeaderElector()
    lease = await elector.try_acquire("role-a")
    assert lease is not None
    assert not lease.lost_event.is_set()
    await asyncio.sleep(0.05)
    assert not lease.lost_event.is_set()
    await lease.release()


@pytest.mark.asyncio
async def test_release_is_idempotent():
    elector = InMemoryLeaderElector()
    lease = await elector.try_acquire("role-a")
    assert lease is not None
    await lease.release()
    await lease.release()  # second call no-ops; doesn't raise
