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


async def test_update_meta_replaces_the_bag_and_none_leaves_it(tree):
    await tree.create(collection_id="c1", parent="", slug="a", body="v1",
                      meta={"summary": "old"})
    res = await tree.read(collection_id="c1", path="a")
    assert res.document.meta == {"summary": "old"}
    await tree.update(collection_id="c1", path="a", meta={"summary": "new"})
    res = await tree.read(collection_id="c1", path="a")
    assert res.document.meta == {"summary": "new"} and res.body == "v1"
    await tree.update(collection_id="c1", path="a", body="v2")
    res = await tree.read(collection_id="c1", path="a")
    assert res.document.meta == {"summary": "new"} and res.body == "v2"


async def test_tree_depth_walk(tree):
    await tree.create(collection_id="c1", parent="", slug="a", body="")
    await tree.create(collection_id="c1", parent="a", slug="b", body="")
    await tree.create(collection_id="c1", parent="a/b", slug="c", body="")
    d1 = await tree.tree(collection_id="c1", parent="", depth=1)
    assert [n.path for n in d1] == ["a"]
    d2 = await tree.tree(collection_id="c1", parent="", depth=2)
    assert [n.path for n in d2] == ["a", "a/b"]


async def test_move_subtree_rewrites_descendant_paths(tree):
    await tree.create(collection_id="c1", parent="", slug="src", body="")
    kid = await tree.create(collection_id="c1", parent="src", slug="kid", body="K")
    await tree.create(collection_id="c1", parent="", slug="dst", body="")
    moved = await tree.move(collection_id="c1", path="src", new_parent="dst")
    assert moved.path == "dst/src"
    res = await tree.read(collection_id="c1", path="dst/src/kid")
    assert res.body == "K" and res.document.id == kid.id  # id stable across moves


async def test_move_into_own_subtree_rejected(tree):
    await tree.create(collection_id="c1", parent="", slug="a", body="")
    await tree.create(collection_id="c1", parent="a", slug="b", body="")
    with pytest.raises(BadRequestError):
        await tree.move(collection_id="c1", path="a", new_parent="a/b")


async def test_delete_requires_recursive_for_children(tree):
    await tree.create(collection_id="c1", parent="", slug="a", body="")
    await tree.create(collection_id="c1", parent="a", slug="b", body="")
    with pytest.raises(ConflictError):
        await tree.delete(collection_id="c1", path="a")
    deleted = await tree.delete(collection_id="c1", path="a", recursive=True)
    assert len(deleted) == 2
    with pytest.raises(NotFoundError):
        await tree.read(collection_id="c1", path="a/b")


async def test_delete_calls_unindexer_per_document(tree):
    seen: list[str] = []

    async def unidx(*, document_id: str, collection_id: str) -> None:
        seen.append(document_id)

    tree._unindexer = unidx
    await tree.create(collection_id="c1", parent="", slug="a", body="")
    await tree.create(collection_id="c1", parent="a", slug="b", body="")
    deleted = await tree.delete(collection_id="c1", path="a", recursive=True)
    assert sorted(seen) == sorted(deleted)


async def test_update_keeps_place_fields_and_hands_the_indexer_the_stored_row(tree):
    """Characterization: an uncontended update writes title, meta, updated_at and the body; the place in the tree is untouched."""
    seen: list = []

    async def idx(*, document, content):
        seen.append((document, content))

    await tree.create(collection_id="c1", parent="", slug="g", body="")
    before = await tree.create(collection_id="c1", parent="g", slug="a", body="v1", title="A", meta={"k": 1})
    tree._indexer = idx
    out = await tree.update(collection_id="c1", path="g/a", body="v2", title="Ay", meta={"k": 2})
    res = await tree.read(collection_id="c1", path="g/a")
    assert (res.body, res.document.title, res.document.meta) == ("v2", "Ay", {"k": 2})
    assert (res.document.parent_id, res.document.slug, res.document.path) == (before.parent_id, "a", "g/a")
    assert res.document.updated_at >= before.updated_at
    assert out.title == "Ay" and seen == [(out, "v2")]


def _move_after_first_resolve(tree, **move):
    """Run ``tree.move(**move)`` right after update's first resolve, before it writes: a move committing in that window."""
    original = tree.resolve
    fired = False

    async def resolve(**kw):
        nonlocal fired
        doc = await original(**kw)
        if not fired:
            fired = True
            await tree.move(**move)
        return doc

    tree.resolve = resolve


@pytest.mark.parametrize("body", [None, "v2"])
async def test_update_racing_a_move_keeps_the_moved_path(tree, body):
    """KN-19f: update used to write back the whole row it read, reverting the move's path and slug (and, with a body, the content row's path)."""
    doc = await tree.create(collection_id="c1", parent="", slug="a", body="v1")
    _move_after_first_resolve(tree, collection_id="c1", path="a", new_parent="", new_slug="b")

    with pytest.raises(NotFoundError):  # as if the move ran first: nothing lives at "a" any more
        await tree.update(collection_id="c1", path="a", body=body, title="T")

    res = await tree.read(collection_id="c1", path="b")
    assert res.document.id == doc.id and (res.document.path, res.document.slug) == ("b", "b")
    assert res.body == "v1" and res.document.title is None
