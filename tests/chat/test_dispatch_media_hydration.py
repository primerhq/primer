"""Media parts referencing an artifact are hydrated to inline data pre-turn."""

from __future__ import annotations

import pytest

from primer.chat.dispatch import ChatDispatchDeps, _hydrate_media_parts
from primer.int.artifact_storage import ArtifactBlob
from primer.model.chat import ImagePart, TextPart


class _MemStore:
    def __init__(self, blobs):
        self._blobs = blobs

    async def get(self, aid):
        return self._blobs.get(aid)


class _Reg:
    def __init__(self, store):
        self._store = store

    async def get_default(self):
        return self._store


def _deps(reg):
    return ChatDispatchDeps(
        storage_provider=None, provider_registry=None, event_bus=None,
        chat_tick_router=None, artifact_storage_registry=reg)


@pytest.mark.asyncio
async def test_hydrates_artifact_part():
    store = _MemStore({"artifact-1": ArtifactBlob(data=b"IMG", mime_type="image/png")})
    deps = _deps(_Reg(store))
    parts = [TextPart(text="look"),
             ImagePart(artifact_id="artifact-1", mime_type="image/png")]
    out = await _hydrate_media_parts(deps, parts)
    assert out[0].text == "look"
    assert out[1].data == b"IMG"
    assert out[1].artifact_id is None


@pytest.mark.asyncio
async def test_noop_without_registry():
    deps = _deps(None)
    parts = [ImagePart(artifact_id="artifact-1", mime_type="image/png")]
    out = await _hydrate_media_parts(deps, parts)
    assert out[0].artifact_id == "artifact-1"  # unchanged


@pytest.mark.asyncio
async def test_noop_when_no_artifact_parts():
    deps = _deps(_Reg(_MemStore({})))
    parts = [TextPart(text="plain")]
    out = await _hydrate_media_parts(deps, parts)
    assert out == parts
