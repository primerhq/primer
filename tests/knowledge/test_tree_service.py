"""DocumentTreeService: slug-path tree over entity rows + content store."""
from __future__ import annotations

import pytest
import pytest_asyncio

from primer.knowledge.tree import DocumentTreeService
from primer.model.except_ import BadRequestError, ConflictError, NotFoundError
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def tree(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    yield DocumentTreeService(provider)
    await provider.aclose()


async def test_create_root_and_child_builds_paths(tree):
    root = await tree.create(collection_id="c1", parent="", slug="guides", body="# Guides")
    child = await tree.create(collection_id="c1", parent="guides", slug="intro", body="hello")
    assert root.path == "guides" and root.parent_id is None
    assert child.path == "guides/intro" and child.parent_id == root.id


async def test_read_returns_children(tree):
    await tree.create(collection_id="c1", parent="", slug="guides", body="idx")
    await tree.create(collection_id="c1", parent="guides", slug="a", body="A")
    await tree.create(collection_id="c1", parent="guides", slug="b", body="B", title="Bee")
    res = await tree.read(collection_id="c1", path="guides")
    assert res.body == "idx"
    assert [(c.slug, c.title) for c in res.children] == [("a", "a"), ("b", "Bee")]


async def test_missing_parent_names_siblings(tree):
    await tree.create(collection_id="c1", parent="", slug="guides", body="x")
    with pytest.raises(NotFoundError) as exc:
        await tree.create(collection_id="c1", parent="nope", slug="a", body="y")
    assert "guides" in str(exc.value)


async def test_duplicate_sibling_slug_conflicts(tree):
    await tree.create(collection_id="c1", parent="", slug="a", body="1")
    with pytest.raises(ConflictError):
        await tree.create(collection_id="c1", parent="", slug="a", body="2")


async def test_strict_slug_enforced_on_create(tree):
    with pytest.raises(BadRequestError):
        await tree.create(collection_id="c1", parent="", slug="Bad.Slug", body="x")


async def test_update_body_and_title(tree):
    await tree.create(collection_id="c1", parent="", slug="a", body="v1")
    await tree.update(collection_id="c1", path="a", body="v2", title="Ay")
    res = await tree.read(collection_id="c1", path="a")
    assert res.body == "v2" and res.document.title == "Ay"
