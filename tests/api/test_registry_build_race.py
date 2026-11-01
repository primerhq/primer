"""GX-4: an invalidate that lands while a get is building is not lost.

The semantic-search, web-search, web-fetch and speech registries read the row and build outside their lock. An eviction in between found
nothing to drop, so the instance built from the pre-edit row was cached and served until restart. Now, as in ProviderRegistry and
ArtifactStorageRegistry, a generation counter makes that get hand its instance to its own caller only: the next get builds from the current row.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from primer.api.registries.semantic_search_registry import SemanticSearchRegistry
from primer.api.registries.speech_registry import SpeechRegistry
from primer.api.registries.web_fetch_registry import WebFetchRegistry
from primer.api.registries.web_search_registry import WebSearchRegistry

pytestmark = pytest.mark.asyncio


class _Rows:
    """A one-row storage whose first get() can be held mid-read (a slow database round trip)."""

    def __init__(self) -> None:
        self.row = SimpleNamespace(id="p1", version="v1")
        self.hold = asyncio.Event()
        self.reading = asyncio.Event()
        self._first = True

    async def get(self, _id: str):
        row = self.row
        if self._first:
            self._first = False
            self.reading.set()
            await self.hold.wait()
        return row


class _Instance:
    def __init__(self, row) -> None:
        self.version = row.version
        self.closed = False

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


def _registries(rows: _Rows):
    ssp = SemanticSearchRegistry(storage=rows, factory=_Instance)
    web_search = WebSearchRegistry(storage=rows, factory=_Instance)
    web_fetch = WebFetchRegistry(storage=rows, factory=_Instance)
    speech = SpeechRegistry(storage=rows, factory=_Instance, label="tts")
    return {
        "ssp": (ssp, ssp.get_provider),
        "web_search": (web_search, web_search.get),
        "web_fetch": (web_fetch, web_fetch.get),
        "speech": (speech, speech.get),
    }


@pytest.mark.parametrize("kind", ["ssp", "web_search", "web_fetch", "speech"])
async def test_an_eviction_during_a_build_is_not_lost(kind):
    rows = _Rows()
    registry, get = _registries(rows)[kind]
    first = asyncio.create_task(get("p1"))
    await rows.reading.wait()                           # mid round trip, nothing cached yet
    rows.row = SimpleNamespace(id="p1", version="v2")   # the edit lands ...
    await registry.invalidate("p1")                     # ... and its eviction finds nothing to drop
    rows.hold.set()
    assert (await first).version == "v1"                # the caller that started before the edit still gets its instance
    assert (await get("p1")).version == "v2"            # every later call sees the edit
    await registry.aclose()


@pytest.mark.parametrize("kind", ["ssp", "web_search", "web_fetch", "speech"])
async def test_a_build_with_no_eviction_is_cached(kind):
    rows = _Rows()
    rows.hold.set()
    registry, get = _registries(rows)[kind]
    built = await get("p1")
    assert await get("p1") is built
    await registry.aclose()
