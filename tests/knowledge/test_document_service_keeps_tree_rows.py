"""The path-addressed document door keeps the tree row it updates and moves like the tree does (review of knowledge, finding 2).

``put_document`` / ``PUT /documents?path=`` used to rebuild an existing row from scratch (parent, title, meta, harness lock lost: it dropped out of the
tree), gave a new nested path no parent, and a path move changed only ``path`` (slug and parent stale, descendants under the old prefix).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from primer.knowledge.document_service import DocumentService
from primer.knowledge.tree import DocumentTreeService
from primer.model.collection import Collection, Document
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
from primer.storage.factory import StorageProviderFactory


@pytest_asyncio.fixture
async def sp(tmp_path):
    p = StorageProviderFactory.create(StorageProviderConfig(provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "t.sqlite")))
    await p.initialize()
    await p.get_content_store().ensure_schema()
    await p.get_storage(Collection).create(Collection(id="kb", description="kb"))
    yield p
    await p.aclose()


async def _guides(sp):
    tree = DocumentTreeService(sp)
    await tree.create(collection_id="kb", parent="", slug="guides", body="g")
    child = await tree.create(collection_id="kb", parent="guides", slug="setup", body="v1", title="Setup Guide", meta={"summary": "s"})
    return tree, child


async def test_an_update_keeps_parent_title_meta_and_created_at(sp):
    tree, child = await _guides(sp)

    await DocumentService(sp).upsert(collection_id="kb", path="guides/setup", content="v2")

    stored = await sp.get_storage(Document).get(child.id)
    assert (stored.parent_id, stored.title, stored.meta, stored.created_at) == (child.parent_id, "Setup Guide", {"summary": "s"}, child.created_at)
    assert stored.updated_at >= child.updated_at
    assert [k.path for k in await tree.tree(collection_id="kb", parent="guides")] == ["guides/setup"], "still listed under its parent"
    assert (await DocumentService(sp).read(collection_id="kb", path="guides/setup")).content == "v2"


async def test_an_update_sets_only_the_fields_the_body_names(sp):
    _tree, child = await _guides(sp)
    svc = DocumentService(sp)

    await svc.upsert(collection_id="kb", path="guides/setup", content="v2", title="Renamed")
    row = await sp.get_storage(Document).get(child.id)
    assert (row.title, row.meta) == ("Renamed", {"summary": "s"})

    await svc.upsert(collection_id="kb", path="guides/setup", content="v3", meta={"summary": "t"})
    row = await sp.get_storage(Document).get(child.id)
    assert (row.title, row.meta) == ("Renamed", {"summary": "t"})


async def test_a_new_nested_path_gets_its_parent(sp):
    tree = DocumentTreeService(sp)
    guides = await tree.create(collection_id="kb", parent="", slug="guides", body="g")

    doc = await DocumentService(sp).upsert(collection_id="kb", path="guides/new", content="x")

    assert doc.parent_id == guides.id and doc.slug == "new"
    assert [k.path for k in await tree.tree(collection_id="kb", parent="guides")] == ["guides/new"]


async def test_a_new_root_path_and_a_path_with_no_parent_row_stay_parentless(sp):
    svc = DocumentService(sp)
    assert (await svc.upsert(collection_id="kb", path="top", content="x")).parent_id is None
    # Characterization: no parent row at that path (an import writes leaves first): written as before, no parent invented.
    assert (await svc.upsert(collection_id="kb", path="orphan/leaf", content="x")).parent_id is None


async def test_a_path_move_updates_slug_parent_and_every_descendant(sp):
    tree = DocumentTreeService(sp)
    a = await tree.create(collection_id="kb", parent="", slug="a", body="a")
    c = await tree.create(collection_id="kb", parent="a", slug="c", body="c")

    await DocumentService(sp).move(collection_id="kb", src="a", dst="b")

    moved, child = await sp.get_storage(Document).get(a.id), await sp.get_storage(Document).get(c.id)
    assert (moved.path, moved.slug) == ("b", "b")
    assert (child.path, child.parent_id) == ("b/c", a.id)
    assert [k.path for k in (await tree.read(collection_id="kb", path="b")).children] == ["b/c"]


async def test_a_path_move_under_another_parent_sets_the_parent(sp):
    tree = DocumentTreeService(sp)
    x = await tree.create(collection_id="kb", parent="", slug="x", body="x")
    leaf = await tree.create(collection_id="kb", parent="", slug="leaf", body="l")

    await DocumentService(sp).move(collection_id="kb", src="leaf", dst="x/leaf2")

    row = await sp.get_storage(Document).get(leaf.id)
    assert (row.path, row.slug, row.parent_id) == ("x/leaf2", "leaf2", x.id)


async def test_a_move_to_a_taken_path_or_from_a_missing_one_still_refuses(sp):
    svc = DocumentService(sp)
    await svc.upsert(collection_id="kb", path="a.md", content="a")
    await svc.upsert(collection_id="kb", path="b.md", content="b")
    with pytest.raises(ConflictError):
        await svc.move(collection_id="kb", src="a.md", dst="b.md")
    with pytest.raises(NotFoundError):
        await svc.move(collection_id="kb", src="nope.md", dst="c.md")


async def test_a_harness_managed_document_is_not_rewritten_through_the_path_door(sp):
    """``create_document`` / ``update_document`` refuse a harness-managed row; the path door now does too, and the row is left as it was."""
    tree = DocumentTreeService(sp)
    doc = await tree.create(collection_id="kb", parent="", slug="managed", body="harness body", title="T")
    docs = sp.get_storage(Document)
    await docs.update(doc.model_copy(update={"harness_id": "hns_1"}))

    with pytest.raises(ConflictError):
        await DocumentService(sp).upsert(collection_id="kb", path="managed", content="user body")

    row = await docs.get(doc.id)
    assert row.harness_id == "hns_1" and (await DocumentService(sp).read(collection_id="kb", path="managed")).content == "harness body"


async def test_a_path_move_into_a_directory_with_no_row_is_kept_as_a_prefix(sp):
    """Characterization: the path door has always been able to move into ``new/`` where nothing lives; no parent row is invented, and the
    slug and descendants still follow."""
    tree = DocumentTreeService(sp)
    a = await tree.create(collection_id="kb", parent="", slug="a", body="a")
    c = await tree.create(collection_id="kb", parent="a", slug="c", body="c")

    await DocumentService(sp).move(collection_id="kb", src="a", dst="new/place")

    moved, child = await sp.get_storage(Document).get(a.id), await sp.get_storage(Document).get(c.id)
    assert (moved.path, moved.slug, moved.parent_id) == ("new/place", "place", None)
    assert (child.path, child.parent_id) == ("new/place/c", a.id)
