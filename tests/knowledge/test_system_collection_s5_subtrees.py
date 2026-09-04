"""S5 P3: the operator's map covers workspaces, providers and how-to."""
from __future__ import annotations

import pytest_asyncio

from primer.knowledge.system_collection import (
    HOW_TO_GUIDES,
    ROOT_INDEX_SLUG,
    SYSTEM_COLLECTION_ID,
    regenerate_system_collection,
)
from primer.knowledge.tree import DocumentTreeService
from primer.model.model_profile import ModelProfile
from primer.model.provider import (
    LLMProvider,
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def sp(tmp_path):
    provider = StorageProviderFactory.create(
        StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=tmp_path / "t.sqlite"),
        )
    )
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield provider
    await provider.aclose()


SUBTREES = {
    "agents", "graphs", "tools", "collections", "docs",
    "workspaces", "providers", "how-to",
}


async def test_every_subtree_is_present_at_the_root(sp):
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    roots = {
        n.slug
        for n in await tree.tree(collection_id=SYSTEM_COLLECTION_ID, depth=1)
    }
    assert SUBTREES <= roots
    assert ROOT_INDEX_SLUG in roots


async def test_the_root_index_names_every_subtree(sp):
    """The operator's entry point: one document that links to all of them."""
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    index = await tree.read(
        collection_id=SYSTEM_COLLECTION_ID, path=ROOT_INDEX_SLUG,
    )
    for subtree in SUBTREES:
        assert f"({subtree})" in index.body, subtree


async def test_providers_subtree_tracks_registry_state(sp):
    await sp.get_storage(LLMProvider).create(
        LLMProvider(
            id="llm-1",
            provider="ollama",
            config={"url": "http://localhost:11434"},
            limits={"max_concurrency": 2},
        )
    )
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="llm-1--qwen",
            description="default",
            provider_id="llm-1",
            model_name="qwen",
            context_length=32000,
        )
    )
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    llm = await tree.read(
        collection_id=SYSTEM_COLLECTION_ID, path="providers/llm/llm-1",
    )
    assert "ollama" in llm.body
    profiles = await tree.read(
        collection_id=SYSTEM_COLLECTION_ID, path="providers/model-profiles",
    )
    assert "llm-1--qwen" in profiles.body


async def test_aggregated_profile_renders_its_pool_not_none_on_none(sp):
    """01a067c4 gate finding #4: an aggregated profile has no model_name/
    provider_id/context_length -- the single-profile label used to
    render this literally as "None on None, context None"."""
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="leaf-a", description="leaf a",
            provider_id="llm-1", model_name="qwen", context_length=32000,
        )
    )
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="leaf-b", description="leaf b",
            provider_id="llm-1", model_name="qwen-fast", context_length=8000,
        )
    )
    await sp.get_storage(ModelProfile).create(
        ModelProfile(
            id="agg-pool", description="a pool", kind="aggregated",
            members=["leaf-a", "leaf-b"],
        )
    )
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    profiles = await tree.read(
        collection_id=SYSTEM_COLLECTION_ID, path="providers/model-profiles",
    )
    assert "None on None" not in profiles.body
    assert "agg-pool" in profiles.body
    assert "leaf-a" in profiles.body
    assert "leaf-b" in profiles.body


async def test_how_to_guides_are_written(sp):
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    index = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="how-to")
    for slug in HOW_TO_GUIDES:
        assert slug in index.body
        page = await tree.read(
            collection_id=SYSTEM_COLLECTION_ID, path=f"how-to/{slug}",
        )
        assert page.body.strip()


async def test_a_removed_provider_disappears_from_the_next_pass(sp):
    await sp.get_storage(LLMProvider).create(
        LLMProvider(
            id="llm-1",
            provider="ollama",
            config={"url": "http://localhost:11434"},
            limits={"max_concurrency": 2},
        )
    )
    await regenerate_system_collection(sp, toolset_providers={})
    await sp.get_storage(LLMProvider).delete("llm-1")
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    from primer.model.except_ import NotFoundError
    import pytest

    with pytest.raises(NotFoundError):
        await tree.read(
            collection_id=SYSTEM_COLLECTION_ID, path="providers/llm/llm-1",
        )
