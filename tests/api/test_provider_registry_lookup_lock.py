"""GX-7: a provider lookup does not wait for another provider's cache miss.

ProviderRegistry took one lock for every lookup, cache hits included, and held it across a miss's storage read, so one slow read stalled
every turn start in the process. The hit now reads the cache with no lock; a miss holds a lock for its own id only (two misses on one id
still build once), and an eviction of that id during the build still keeps the stale adapter out of the cache.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from primer.api.registries.provider_registry import ProviderRegistry

pytestmark = pytest.mark.asyncio


class _Storage:
    def __init__(self) -> None:
        self.version = "v1"
        self.hold: dict[str, asyncio.Event] = {}
        self.reading: dict[str, asyncio.Event] = {}
        self.reads = 0

    async def get(self, provider_id: str):
        self.reads += 1
        row = SimpleNamespace(id=provider_id, version=self.version)
        if provider_id in self.hold:
            self.reading.setdefault(provider_id, asyncio.Event()).set()
            await self.hold[provider_id].wait()
        return row


class _SP:
    def __init__(self) -> None:
        self.storage = _Storage()

    def get_storage(self, _model):
        return self.storage


class _Adapter:
    def __init__(self, row) -> None:
        self.id, self.version = row.id, row.version

    async def aclose(self) -> None:
        return None


def _held(sp: _SP, provider_id: str) -> asyncio.Event:
    sp.storage.hold[provider_id] = asyncio.Event()
    sp.storage.reading[provider_id] = asyncio.Event()
    return sp.storage.hold[provider_id]


async def test_a_cache_hit_does_not_wait_for_another_providers_miss():
    sp = _SP()
    registry = ProviderRegistry(sp, llm_factory=_Adapter, embedder_factory=_Adapter)
    fast = await registry.get_llm("fast")
    release = _held(sp, "slow")
    miss = asyncio.create_task(registry.get_llm("slow"))
    await sp.storage.reading["slow"].wait()
    assert await asyncio.wait_for(registry.get_llm("fast"), 1) is fast
    assert (await asyncio.wait_for(registry.get_embedder("other"), 1)).id == "other"   # another miss is not held up either
    release.set()
    assert (await miss).id == "slow"


async def test_two_misses_on_one_id_build_once():
    sp = _SP()
    registry = ProviderRegistry(sp, llm_factory=_Adapter)
    release = _held(sp, "p1")
    first = asyncio.create_task(registry.get_llm("p1"))
    second = asyncio.create_task(registry.get_llm("p1"))
    await sp.storage.reading["p1"].wait()
    release.set()
    assert await first is await second
    assert sp.storage.reads == 1


async def test_an_eviction_of_the_id_during_its_build_is_not_lost():
    sp = _SP()
    registry = ProviderRegistry(sp, llm_factory=_Adapter)
    release = _held(sp, "p1")
    first = asyncio.create_task(registry.get_llm("p1"))
    await sp.storage.reading["p1"].wait()
    sp.storage.version = "v2"
    await registry.invalidate_llm("p1")
    release.set()
    assert (await first).version == "v1"            # the caller that started before the edit completes
    del sp.storage.hold["p1"]
    assert (await registry.get_llm("p1")).version == "v2"
