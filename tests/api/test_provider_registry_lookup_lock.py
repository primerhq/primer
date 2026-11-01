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
    closed = 0

    def __init__(self, row) -> None:
        self.id, self.version = row.id, row.version

    async def aclose(self) -> None:
        self.closed += 1


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


# An adapter built while an eviction or a flush landed is returned to its caller but not cached; it is retired so its client is closed once idle.
IDLE = 0.05


async def _evicted_during_build(registry, sp, get, invalidate):
    release = _held(sp, "p1")
    first = asyncio.create_task(get("p1"))
    await sp.storage.reading["p1"].wait()
    await invalidate("p1")
    release.set()
    adapter = await first
    del sp.storage.hold["p1"]
    return adapter


async def test_an_llm_adapter_built_across_an_eviction_is_retired_once_idle():
    sp = _SP()
    registry = ProviderRegistry(sp, llm_factory=_Adapter, retire_idle_seconds=IDLE)
    adapter = await _evicted_during_build(registry, sp, registry.get_llm, registry.invalidate_llm)
    assert adapter.closed == 0                                # still usable by the caller it was returned to
    await asyncio.sleep(IDLE * 6)
    assert adapter.closed == 1


async def test_an_embedder_built_across_an_eviction_is_retired_once_idle():
    sp = _SP()
    registry = ProviderRegistry(sp, embedder_factory=_Adapter, retire_idle_seconds=IDLE)
    adapter = await _evicted_during_build(registry, sp, registry.get_embedder, registry.invalidate_embedder)
    await asyncio.sleep(IDLE * 6)
    assert adapter.closed == 1


async def test_a_cached_adapter_is_not_retired_by_the_lookup_that_built_it():
    sp = _SP()
    registry = ProviderRegistry(sp, llm_factory=_Adapter, retire_idle_seconds=IDLE)
    adapter = await registry.get_llm("p1")
    await asyncio.sleep(IDLE * 6)
    assert adapter.closed == 0 and await registry.get_llm("p1") is adapter


async def test_a_toolset_built_across_an_eviction_is_not_retired():
    """Toolset providers are not tracked for in-flight calls, so retiring one would close it under a call after the idle period."""
    sp = _SP()
    registry = ProviderRegistry(sp, toolset_factory=_Adapter, retire_idle_seconds=IDLE)
    adapter = await _evicted_during_build(registry, sp, registry.get_toolset, registry.invalidate_toolset)
    await asyncio.sleep(IDLE * 6)
    assert adapter.closed == 0
