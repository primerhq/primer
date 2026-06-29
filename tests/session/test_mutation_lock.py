"""Unit tests for the per-session lifecycle KeyedLock (T0432)."""

from __future__ import annotations

import asyncio

import pytest

from primer.session.mutation_lock import KeyedLock, session_lifecycle_lock


@pytest.mark.asyncio
async def test_same_key_is_mutually_exclusive() -> None:
    """Two coroutines sharing a key never overlap their critical sections."""
    kl = KeyedLock()
    order: list[str] = []

    async def worker(tag: str, hold: float) -> None:
        async with kl.acquire("k"):
            order.append(f"{tag}-enter")
            await asyncio.sleep(hold)
            order.append(f"{tag}-exit")

    # A is scheduled first and holds the lock across a sleep; B must wait
    # for A to fully exit before it can enter.
    await asyncio.gather(worker("A", 0.05), worker("B", 0.0))

    assert order == ["A-enter", "A-exit", "B-enter", "B-exit"]


@pytest.mark.asyncio
async def test_distinct_keys_do_not_contend() -> None:
    """Different keys run concurrently — no false serialization."""
    kl = KeyedLock()
    order: list[str] = []

    async def worker(tag: str, key: str, hold: float) -> None:
        async with kl.acquire(key):
            order.append(f"{tag}-enter")
            await asyncio.sleep(hold)
            order.append(f"{tag}-exit")

    # B (key 'kb') enters and exits while A (key 'ka') is still sleeping.
    await asyncio.gather(worker("A", "ka", 0.05), worker("B", "kb", 0.0))

    assert order.index("B-exit") < order.index("A-exit")


@pytest.mark.asyncio
async def test_lock_object_is_dropped_after_last_release() -> None:
    """No per-key leak: the lock + refcount are cleaned up on last release."""
    kl = KeyedLock()
    async with kl.acquire("k"):
        assert "k" in kl._locks
        assert kl._refs["k"] == 1
    assert "k" not in kl._locks
    assert "k" not in kl._refs


@pytest.mark.asyncio
async def test_waiter_keeps_lock_object_alive() -> None:
    """While a second coroutine waits, the shared lock object must survive
    the first holder's release bookkeeping (refcount counts the waiter)."""
    kl = KeyedLock()
    acquired = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with kl.acquire("k"):
            acquired.set()
            await release.wait()

    async def second() -> None:
        async with kl.acquire("k"):
            pass

    t1 = asyncio.create_task(first())
    await acquired.wait()              # first holds the lock (refcount 1)
    t2 = asyncio.create_task(second())
    await asyncio.sleep(0.01)          # second bumps refcount to 2, then blocks

    assert kl._refs["k"] == 2          # the waiter is counted → lock kept alive

    release.set()
    await asyncio.gather(t1, t2)

    # Both released → the shared object is cleaned up (no per-key leak).
    assert "k" not in kl._locks
    assert "k" not in kl._refs


@pytest.mark.asyncio
async def test_cleans_up_when_body_raises() -> None:
    """An exception in the guarded body still releases + cleans up."""
    kl = KeyedLock()
    with pytest.raises(ValueError):
        async with kl.acquire("k"):
            raise ValueError("boom")
    assert "k" not in kl._locks
    assert "k" not in kl._refs


def test_session_lifecycle_lock_is_a_process_singleton() -> None:
    """The handlers must share one lock instance to serialize each other."""
    assert session_lifecycle_lock() is session_lifecycle_lock()
    assert isinstance(session_lifecycle_lock(), KeyedLock)
