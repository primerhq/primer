"""ServiceResolver cache bounds (UI-SEC-03).

The resolver caches negative results so a scanner cannot hammer
storage, but the cache must stay bounded: an anonymous caller can mint
unlimited unknown names, and expired entries must not linger.
"""

from __future__ import annotations

import pytest

from primer.service.serve import ServiceResolver


class _Page:
    def __init__(self) -> None:
        self.items: list = []


class _Storage:
    def __init__(self) -> None:
        self.finds = 0

    async def find(self, predicate, page):  # noqa: ARG002
        self.finds += 1
        return _Page()


class _SP:
    def __init__(self) -> None:
        self.storage = _Storage()

    def get_storage(self, model):  # noqa: ARG002
        return self.storage


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_cache_is_bounded_by_entry_count():
    sp = _SP()
    resolver = ServiceResolver(sp, max_entries=8, clock=_Clock())
    for i in range(100):
        assert await resolver.resolve(f"unknown-{i}") is None
    assert len(resolver._cache) == 8
    # The most recent names survive; the oldest were evicted.
    assert "unknown-99" in resolver._cache
    assert "unknown-0" not in resolver._cache


@pytest.mark.asyncio
async def test_negative_entries_are_still_cached_within_ttl():
    sp = _SP()
    resolver = ServiceResolver(sp, ttl_seconds=5.0, clock=_Clock())
    await resolver.resolve("ghost")
    await resolver.resolve("ghost")
    assert sp.storage.finds == 1


@pytest.mark.asyncio
async def test_expired_entries_are_evicted():
    sp = _SP()
    clock = _Clock()
    resolver = ServiceResolver(
        sp, ttl_seconds=5.0, max_entries=1000, clock=clock
    )
    for i in range(50):
        await resolver.resolve(f"old-{i}")
    assert len(resolver._cache) == 50
    clock.now = 10.0
    await resolver.resolve("fresh")
    # Every expired negative entry is gone, not just overwritten on reuse.
    assert list(resolver._cache) == ["fresh"]
    assert sp.storage.finds == 51
