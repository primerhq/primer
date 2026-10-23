"""S5 P4: the tool reference follows the live registry, so the operator cannot rot."""
from __future__ import annotations

import pytest
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


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    """A hand-written wiki plus an empty ai-docs dir, pinned by env vars."""
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "index.md").write_text(
        "---\n"
        "title: Platform wiki\n"
        "summary: The tracks test wiki root.\n"
        "---\n"
        "\n"
        "The tracks wiki.\n",
        encoding="utf-8",
    )
    docs = tmp_path / "ai-docs"
    docs.mkdir()
    monkeypatch.setenv("PRIMER_WIKI_DIR", str(root))
    monkeypatch.setenv("PRIMER_AI_DOCS_DIR", str(docs))
    monkeypatch.setattr(
        "primer.knowledge.system_collection.RESERVED_TOOLSET_IDS",
        frozenset(),
    )
    return root


async def test_a_new_toolset_shows_up_on_the_next_pass(sp, wiki):
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    from primer.model.except_ import NotFoundError
    import pytest

    with pytest.raises(NotFoundError):
        await tree.read(
            collection_id=SYSTEM_COLLECTION_ID,
            path="reference/tools/crud/create_agent",
        )

    await regenerate_system_collection(
        sp, toolset_providers={"crud": build_crud_toolset(storage_provider=sp)},
    )
    page = await tree.read(
        collection_id=SYSTEM_COLLECTION_ID,
        path="reference/tools/crud/create_agent",
    )
    assert page.body.strip()
    index = await tree.read(
        collection_id=SYSTEM_COLLECTION_ID, path="reference/tools/crud",
    )
    assert "create_agent" in index.body and "update_trigger" in index.body


async def test_a_removed_toolset_is_pruned(sp, wiki):
    await regenerate_system_collection(
        sp, toolset_providers={"crud": build_crud_toolset(storage_provider=sp)},
    )
    await regenerate_system_collection(sp, toolset_providers={})
    tree = DocumentTreeService(sp)
    from primer.model.except_ import NotFoundError
    import pytest

    with pytest.raises(NotFoundError):
        await tree.read(
            collection_id=SYSTEM_COLLECTION_ID, path="reference/tools/crud",
        )
