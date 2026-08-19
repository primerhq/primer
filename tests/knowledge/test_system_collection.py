"""Unconditional system-collection regeneration (amendment M12, decision D-B)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from primer.knowledge.system_collection import (
    SYSTEM_COLLECTION_ID, regenerate_system_collection,
)
from primer.knowledge.tree import DocumentTreeService
from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection
from primer.model.except_ import NotFoundError
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def sp(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield provider
    await provider.aclose()


async def test_regeneration_builds_subtrees_without_any_embedder(sp):
    await sp.get_storage(Agent).create(
        Agent(id="agent-a", description="helper",
              model=AgentModel(profile_id="prov--m"))
    )
    n = await regenerate_system_collection(sp, toolset_providers={})
    assert n > 0
    coll = await sp.get_storage(Collection).get(SYSTEM_COLLECTION_ID)
    assert coll is not None and coll.system is True and coll.search is None
    tree = DocumentTreeService(sp)
    res = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="agents/agent-a")
    assert "helper" in res.body
    idx = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="agents")
    assert "agent-a" in idx.body


async def test_regeneration_is_idempotent_and_prunes_stale(sp):
    agents = sp.get_storage(Agent)
    await agents.create(Agent(id="agent-a", description="helper",
                              model=AgentModel(profile_id="prov--m")))
    await regenerate_system_collection(sp, toolset_providers={})
    await agents.delete("agent-a")
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    with pytest.raises(NotFoundError):
        await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="agents/agent-a")
