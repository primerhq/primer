"""Path-addressed document tools on the ``_system`` toolset.

Covers the Task 12 rework of ``get_document_content`` / ``put_document`` to
address documents by ``(collection_id, path)`` via :class:`DocumentService`
(bodies in the content store, NOT ``meta['content']``) plus the new
``list_documents`` and ``move_document`` tools. The toolset is built over a
REAL sqlite provider so the content store + transactional writes exercise
their real code paths.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from primer.api.registries import ProviderRegistry
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory
from primer.toolset.system import build_system_toolset


@pytest_asyncio.fixture
async def tools(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    sp = StorageProviderFactory.create(cfg)
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    pr = ProviderRegistry(
        sp,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    provider = build_system_toolset(storage_provider=sp, provider_registry=pr)
    pr._system_toolset_provider = provider  # type: ignore[attr-defined]
    # A collection to hang documents off (search off -> indexer is a no-op).
    await provider.call(
        tool_name="create_collection",
        arguments={
            "entity": Collection(
                id="c1",
                description="test",
                embedder=CollectionEmbedder(provider_id="hf-1", model="m"),
                search_provider_id="ssp-1",
            ).model_dump(mode="json")
        },
    )
    yield provider, sp
    await sp.aclose()


async def call(provider, name: str, args: dict):
    return await provider.call(tool_name=name, arguments=args)


async def test_put_then_get_by_path(tools):
    provider, sp = tools
    r = await call(
        provider,
        "put_document",
        {"collection_id": "c1", "path": "a/b.md", "content": "hello", "title": "B"},
    )
    assert not r.is_error, r.output
    g = await call(
        provider, "get_document_content", {"collection_id": "c1", "path": "a/b.md"}
    )
    assert not g.is_error, g.output
    body = json.loads(g.output)
    assert body["content"] == "hello"
    assert body["path"] == "a/b.md"
    assert body["title"] == "B"

    # The body lives in the content store, NOT in meta['content']: fetch the
    # entity row and assert meta carries no 'content' key.
    doc_id = body["id"]
    from primer.model.collection import Document

    entity = await sp.get_storage(Document).get(doc_id)
    assert entity is not None
    assert "content" not in (entity.meta or {})


async def test_put_empty_content(tools):
    provider, _sp = tools
    r = await call(
        provider,
        "put_document",
        {"collection_id": "c1", "path": "empty.md", "content": ""},
    )
    assert not r.is_error, r.output
    g = await call(
        provider, "get_document_content", {"collection_id": "c1", "path": "empty.md"}
    )
    assert not g.is_error, g.output
    assert json.loads(g.output)["content"] == ""


async def test_list_documents(tools):
    provider, _sp = tools
    for path in ("a/b.md", "a/c.md", "other/d.md"):
        r = await call(
            provider,
            "put_document",
            {"collection_id": "c1", "path": path, "content": "x"},
        )
        assert not r.is_error, r.output
    res = await call(
        provider, "list_documents", {"collection_id": "c1", "prefix": "a/"}
    )
    assert not res.is_error, res.output
    payload = json.loads(res.output)
    paths = {d["path"] for d in payload["documents"]}
    assert paths == {"a/b.md", "a/c.md"}
    # No bodies in the listing.
    assert all("content" not in d for d in payload["documents"])


async def test_move_document(tools):
    provider, _sp = tools
    r = await call(
        provider, "put_document", {"collection_id": "c1", "path": "a.md", "content": "x"}
    )
    assert not r.is_error, r.output
    mv = await call(
        provider, "move_document", {"collection_id": "c1", "from": "a.md", "to": "b.md"}
    )
    assert not mv.is_error, mv.output
    gone = await call(
        provider, "get_document_content", {"collection_id": "c1", "path": "a.md"}
    )
    assert gone.is_error
    assert json.loads(gone.output)["type"] == "not-found"
    moved = await call(
        provider, "get_document_content", {"collection_id": "c1", "path": "b.md"}
    )
    assert not moved.is_error, moved.output
    assert json.loads(moved.output)["content"] == "x"


async def test_get_missing_is_not_found_tool_error(tools):
    provider, _sp = tools
    g = await call(
        provider, "get_document_content", {"collection_id": "c1", "path": "nope.md"}
    )
    assert g.is_error
    assert json.loads(g.output)["type"] == "not-found"


async def test_move_conflict_tool_error(tools):
    provider, _sp = tools
    for path in ("a.md", "b.md"):
        r = await call(
            provider,
            "put_document",
            {"collection_id": "c1", "path": path, "content": "x"},
        )
        assert not r.is_error, r.output
    mv = await call(
        provider, "move_document", {"collection_id": "c1", "from": "b.md", "to": "a.md"}
    )
    assert mv.is_error
    assert json.loads(mv.output)["type"] == "conflict"
