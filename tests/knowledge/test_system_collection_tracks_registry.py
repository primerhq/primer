"""S5 P4: /tools follows the live registry, so the operator cannot rot."""
from __future__ import annotations

import pytest_asyncio

from primer.knowledge.system_collection import (
    SYSTEM_COLLECTION_ID,
    regenerate_system_collection,
)
from primer.knowledge.tree import DocumentTreeService
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory
from primer.toolset.crud import build_crud_toolset


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


async def test_a_new_toolset_shows_up_on_the_next_pass(sp):
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    from primer.model.except_ import NotFoundError
    import pytest

    with pytest.raises(NotFoundError):
        await tree.read(
            collection_id=SYSTEM_COLLECTION_ID, path="tools/crud/create_agent",
        )

    await regenerate_system_collection(
        sp, toolset_providers={"crud": build_crud_toolset(storage_provider=sp)},
    )
    page = await tree.read(
        collection_id=SYSTEM_COLLECTION_ID, path="tools/crud/create_agent",
    )
    assert page.body.strip()
    index = await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="tools/crud")
    assert "create_agent" in index.body and "update_trigger" in index.body


async def test_a_removed_toolset_is_pruned(sp):
    await regenerate_system_collection(
        sp, toolset_providers={"crud": build_crud_toolset(storage_provider=sp)},
    )
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    from primer.model.except_ import NotFoundError
    import pytest

    with pytest.raises(NotFoundError):
        await tree.read(collection_id=SYSTEM_COLLECTION_ID, path="tools/crud")
