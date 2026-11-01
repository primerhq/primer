"""Provisioning of an agent's memory collection (agent memory Task 2)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import primer.memory.collection as mc
from primer.knowledge.tree import DocumentTreeService
from primer.memory.collection import (
    MEMORY_PREFIX, delete_memory_collection, ensure_memory_collection, memory_access, memory_collection_id, purge_memory_entries,
)
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection, CollectionEmbedder, CollectionSearchConfig, Document
from primer.model.internal import INTERNAL_COLLECTIONS_CONFIG_ID, InternalCollectionsConfig
from primer.model.memory import AgentMemory
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.storage.sqlite import SqliteStorageProvider

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def sp(tmp_path: Path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "m.sqlite"))
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield provider
    await provider.aclose()


def _agent() -> Agent:
    return Agent(id="a1", description="d", model=AgentModel(profile_id="p"), access=Access(owners=["u1"], grants={"u2": "read"}))


@pytest.fixture
def enabled(monkeypatch):
    """enable_search recorded, not run (it needs real providers): it stamps the search block as a finished enable would."""
    calls: list[CollectionSearchConfig] = []

    async def fake(sp, provider_registry, ssr, *, collection_id, cfg, person=None):
        calls.append(cfg)
        row = await sp.get_storage(Collection).get(collection_id)
        return await sp.get_storage(Collection).update(row.model_copy(update={"search": cfg.model_copy(update={"state": "ready"})}))

    monkeypatch.setattr(mc, "enable_search", fake)
    return calls


async def test_ids_and_prefix() -> None:
    assert MEMORY_PREFIX == "mem-" and memory_collection_id("a1") == "mem-a1"


async def test_ensure_is_idempotent_and_sets_memory_of(sp, enabled) -> None:
    first = await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1"))
    again = await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1"))
    assert first.id == again.id == "mem-a1" and again.memory_of == "a1"
    assert (await sp.get_storage(Collection).list(OffsetPage(offset=0, length=10))).total == 1


async def test_ensure_without_any_providers_leaves_search_none(sp, enabled) -> None:
    row = await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1"))
    assert row.search is None and enabled == []


async def test_ensure_uses_internal_collections_providers_when_settings_search_is_none(sp, enabled) -> None:
    await sp.get_storage(InternalCollectionsConfig).create(InternalCollectionsConfig(
        id=INTERNAL_COLLECTIONS_CONFIG_ID, embedding_provider_id="emb", embedding_model="m", search_provider_id="ssp",
    ))
    row = await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1"))
    assert enabled[0].embedder.provider_id == "emb" and enabled[0].embedder.model == "m" and enabled[0].vector_store_provider_id == "ssp"
    assert row.search is not None and row.search.state == "ready"
    await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1"))
    assert len(enabled) == 1, "an unchanged search is not enabled again"


async def test_settings_search_beats_the_install_default(sp, enabled) -> None:
    cfg = CollectionSearchConfig(embedder=CollectionEmbedder(provider_id="mine", model="mm"), vector_store_provider_id="vs")
    row = await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1", search=cfg))
    assert enabled[0].embedder.provider_id == "mine" and row.search.vector_store_provider_id == "vs"


async def test_memory_access_is_the_agents(sp, enabled) -> None:
    await sp.get_storage(Agent).create(_agent())
    row = await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1"))
    access = await memory_access(sp, row)
    assert access.owners == ["u1"] and access.grants == {"u2": "read"}


async def test_memory_access_of_a_missing_agent_is_nobody(sp) -> None:
    orphan = Collection(id="mem-gone", description="d", memory_of="gone")
    access = await memory_access(sp, orphan)
    assert access.owners == [] and access.grants == {} and not access.public


async def test_purge_empties_the_entries_and_keeps_the_collection(sp, enabled) -> None:
    await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1"))
    tree = DocumentTreeService(sp)
    await tree.create(collection_id="mem-a1", parent="", slug="active", body="")
    await tree.create(collection_id="mem-a1", parent="active", slug="e1", body="x")
    assert await purge_memory_entries(sp, "a1") == 2
    assert (await sp.get_storage(Document).list(OffsetPage(offset=0, length=10))).items == []
    assert await sp.get_storage(Collection).get("mem-a1") is not None
    assert await purge_memory_entries(sp, "nobody") == 0


async def test_delete_removes_entries_and_the_collection(sp, enabled) -> None:
    await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1"))
    await DocumentTreeService(sp).create(collection_id="mem-a1", parent="", slug="active", body="")
    await delete_memory_collection(sp, None, "a1")
    assert await sp.get_storage(Collection).get("mem-a1") is None
    assert (await sp.get_storage(Document).list(OffsetPage(offset=0, length=10))).items == []
    await delete_memory_collection(sp, None, "a1")      # idempotent


async def test_the_live_map_leaves_memory_collections_out(sp, enabled) -> None:
    from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID, _listed, regenerate_system_collection

    await sp.get_storage(Collection).create(Collection(id="kb", description="wiki", access=Access(public=True)))
    row = await ensure_memory_collection(sp, None, None, _agent(), AgentMemory(id="a1"))
    assert _listed(row) is False and _listed(Collection(id="kb", description="d", access=Access(public=True))) is True
    await regenerate_system_collection(sp, toolset_providers={})
    index = (await DocumentTreeService(sp).read(collection_id=SYSTEM_COLLECTION_ID, path="this-install/collections")).body
    assert "kb" in index and "mem-a1" not in index
    pages = await DocumentTreeService(sp).tree(collection_id=SYSTEM_COLLECTION_ID, parent="this-install/collections", depth=1)
    assert [n.slug for n in pages] == ["kb"]
