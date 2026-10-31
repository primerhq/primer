"""ArtifactLRU: sequential behaviour is pinned by the characterization tests;
the concurrent-miss tests assert the accounting after a race (KN-16).

A miss awaits the artifact store before inserting, so two requests for the
same asset can both miss and both insert. The cache must hold ONE copy and
count its size ONCE: the phantom bytes otherwise fill the cap until every
blob is evicted on insert and every request goes to artifact storage, until
restart.
"""

from __future__ import annotations

import asyncio

from primer.int.artifact_storage import ArtifactBlob
from primer.service.serve import ArtifactLRU


class _Store:
    """An artifact store that yields once per get so concurrent misses race."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs
        self.calls: list[str] = []

    async def get(self, artifact_id: str) -> ArtifactBlob | None:
        await asyncio.sleep(0.01)
        self.calls.append(artifact_id)
        data = self._blobs.get(artifact_id)
        if data is None:
            return None
        return ArtifactBlob(data=data, mime_type="text/plain", filename=artifact_id)


# ---- characterization: sequential access pins today's behaviour ------------


async def test_miss_inserts_and_the_next_get_is_a_hit_without_reaching_storage():
    store = _Store({"a": b"x" * 100})
    lru = ArtifactLRU(max_bytes= 250)
    first = await lru.get(store, "a")
    assert first is not None and first.data == b"x" * 100
    assert store.calls == ["a"]
    second = await lru.get(store, "a")
    assert second is first  # the cached entry, not a fresh blob
    assert store.calls == ["a"]  # a hit never reaches storage


async def test_eviction_is_lru_and_the_running_total_tracks_the_cached_bytes():
    store = _Store({"a": b"a" * 100, "b": b"b" * 100})
    lru = ArtifactLRU(max_bytes=250)
    for aid in ("a", "b"):
        await lru.get(store, aid)
    assert lru._total == 200
    assert list(lru._entries) == ["a", "b"]

    # touching "a" makes it most recent, so the next insert evicts "b"
    await lru.get(store, "a")
    store._blobs["c"] = b"c" * 100
    await lru.get(store, "c")
    assert lru._total == 200
    assert list(lru._entries) == ["a", "c"]


async def test_a_blob_larger_than_the_cap_is_served_but_not_cached():
    store = _Store({"big": b"y" * 300})
    lru = ArtifactLRU(max_bytes=250)
    blob = await lru.get(store, "big")
    assert blob is not None and len(blob.data) == 300
    assert lru._entries == {} and lru._total == 0


async def test_an_unknown_id_returns_none_and_is_not_cached():
    store = _Store({})
    lru = ArtifactLRU(max_bytes=250)
    assert await lru.get(store, "ghost") is None
    assert lru._entries == {} and lru._total == 0


# ---- KN-16: concurrent misses must not double-count the size ---------------


async def test_concurrent_misses_count_the_blob_once_and_the_cache_keeps_its_capacity():
    """Two races of two parallel misses (the probe, inverted).

    Today each miss inserts and adds its size again: after two races the
    cache holds no usable capacity yet counts 200 of its 250 bytes, and a
    fresh 100-byte blob is evicted on insert.
    """
    store = _Store({"a1": b"x" * 100, "a2": b"y" * 100, "b": b"z" * 100})
    lru = ArtifactLRU(max_bytes=250)
    await asyncio.gather(*(lru.get(store, "a1") for _ in range(2)))
    assert len(lru._entries) == 1 and lru._total == 100  # one copy, counted once
    await asyncio.gather(*(lru.get(store, "a2") for _ in range(2)))
    assert lru._total == 200
    # a 100-byte blob in a 250-byte cache must be kept: no phantom bytes
    await lru.get(store, "b")
    assert "b" in lru._entries
