"""collections toolset: navigation ergonomics ARE the contract."""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from primer.knowledge.tree import DocumentTreeService
from primer.model.collection import Collection
from primer.model.collection import (
    CollectionEmbedder,
    CollectionSearchConfig,
)
from primer.model.provider import (
    SqliteConfig, StorageProviderConfig, StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory
from primer.toolset.collections import build_collections_toolset


@pytest_asyncio.fixture
async def env(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    colls = provider.get_storage(Collection)
    await colls.create(Collection(id="collection-u", description="user wiki"))
    await colls.create(Collection(id="collection-s", description="sys", system=True))
    tree = DocumentTreeService(provider)
    await tree.create(collection_id="collection-u", parent="", slug="guides",
                      body="# G\nneedle")
    ts = build_collections_toolset(
        storage_provider=provider,
        provider_registry=None,
        semantic_search_registry=None,
    )
    yield ts
    await provider.aclose()


async def test_collections_list(env):
    res = await env.call(tool_name="collections_list", arguments={})
    rows = json.loads(res.output)["collections"]
    assert {r["id"]: r["system"] for r in rows} == {
        "collection-u": False, "collection-s": True,
    }
    assert all(r["search_enabled"] is False for r in rows)


async def test_read_document_returns_children(env):
    res = await env.call(
        tool_name="read_document",
        arguments={"collection": "collection-u", "path": "guides"},
    )
    data = json.loads(res.output)
    assert data["body"].startswith("# G")
    assert data["children"] == []


async def test_missing_path_names_alternatives(env):
    res = await env.call(
        tool_name="read_document",
        arguments={"collection": "collection-u", "path": "gudes"},
    )
    assert res.is_error
    assert "guides" in json.loads(res.output)["message"]


async def test_grep_carries_truncated_flag(env):
    res = await env.call(tool_name="grep_collection", arguments={
        "collection": "collection-u", "pattern": "needle", "max_results": 1,
    })
    data = json.loads(res.output)
    assert data["hits"][0]["path"] == "guides"
    assert data["truncated"] is False


async def test_semantic_search_disabled_is_informative(env):
    res = await env.call(
        tool_name="semantic_search",
        arguments={"collection": "collection-u", "query": "x"},
    )
    assert res.is_error
    msg = json.loads(res.output)["message"]
    assert "not enabled" in msg and "grep_collection" in msg


async def test_system_collection_write_forbidden(env):
    res = await env.call(tool_name="create_document", arguments={
        "collection": "collection-s", "parent": "", "slug": "a", "body": "x",
    })
    assert res.is_error
    assert json.loads(res.output)["type"] == "forbidden"


# ---- the unified search tool ---------------------------------------------


async def test_search_auto_uses_fulltext_when_no_semantic(env):
    res = await env.call(tool_name="search", arguments={
        "collection": "collection-u", "query": "needle",
    })
    data = json.loads(res.output)
    assert data["mode_used"] == "fulltext"
    assert data["hits"][0]["path"] == "guides"
    assert "score" in data["hits"][0]


async def test_search_literal_mode_escapes_regex_metachars(env):
    await env.call(tool_name="create_document", arguments={
        "collection": "collection-u", "slug": "notes",
        "body": "a(b) literally",
    })
    res = await env.call(tool_name="search", arguments={
        "collection": "collection-u", "query": "a(b)", "mode": "literal",
    })
    data = json.loads(res.output)
    assert data["mode_used"] == "literal"
    assert data["hits"][0]["path"] == "notes"
    assert data["hits"][0]["line"] == 1


async def test_search_regex_mode_is_a_pattern(env):
    res = await env.call(tool_name="search", arguments={
        "collection": "collection-u", "query": "need.e", "mode": "regex",
    })
    data = json.loads(res.output)
    assert data["mode_used"] == "regex"
    assert data["hits"][0]["path"] == "guides"


async def test_search_explicit_semantic_without_index_names_alternatives(env):
    res = await env.call(tool_name="search", arguments={
        "collection": "collection-u", "query": "anything",
        "mode": "semantic",
    })
    assert res.is_error
    msg = json.loads(res.output)["message"]
    assert "available: fulltext, literal, regex" in msg


@pytest_asyncio.fixture
async def env_broken(tmp_path):
    """A collection whose semantic index is in error state."""
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "b.sqlite"),
    )
    provider = StorageProviderFactory.create(cfg)
    await provider.initialize()
    await provider.get_content_store().ensure_schema()
    await provider.get_storage(Collection).create(Collection(
        id="collection-b", description="broken index",
        search=CollectionSearchConfig(
            embedder=CollectionEmbedder(provider_id="e", model="m"),
            vector_store_provider_id="v",
            state="error", error="index exploded",
        ),
    ))
    tree = DocumentTreeService(provider)
    await tree.create(collection_id="collection-b", parent="", slug="doc",
                      body="refund text")
    ts = build_collections_toolset(
        storage_provider=provider,
        provider_registry=None,
        semantic_search_registry=None,
    )
    yield ts
    await provider.aclose()


async def test_search_auto_degrades_and_says_so(env_broken):
    res = await env_broken.call(tool_name="search", arguments={
        "collection": "collection-b", "query": "refund",
    })
    data = json.loads(res.output)
    assert data["mode_used"] == "fulltext"
    assert "semantic" in data["note"]
    assert data["hits"][0]["path"] == "doc"
