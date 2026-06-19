"""Unit tests for the legacy document-body content migration.

Seeds RAW ``document`` rows (bypassing :class:`Document` validation, since
legacy rows predate the now-required ``path`` field) into a real sqlite
provider, then drives :func:`primer.knowledge.migration.migrate_document_content`
and asserts each non-system document's body lands in the content store with a
unique path + title, that system collections are skipped, that re-running is a
no-op, and that same-name documents get distinct paths.
"""

from __future__ import annotations

import json

import pytest_asyncio

from primer.knowledge.migration import migrate_document_content
from primer.model.collection import Collection, CollectionEmbedder, Document
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


async def _raw_insert_document(provider, data: dict) -> None:
    """Insert a legacy ``document`` row at the raw SQL level.

    Mirrors :meth:`SqliteStorage.create`'s row shape (id column hoisted out
    of the JSON ``data`` blob) without going through ``Storage[Document]`` --
    legacy rows have no ``path`` and would fail Document validation.
    """
    conn = provider.connection
    await conn.execute(
        'CREATE TABLE IF NOT EXISTS "document" ('
        "id TEXT PRIMARY KEY, "
        "data TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    doc_id = data["id"]
    payload = {k: v for k, v in data.items() if k != "id"}
    await conn.execute(
        'INSERT INTO "document" (id, data) VALUES (?, ?)',
        (doc_id, json.dumps(payload, separators=(",", ":"))),
    )
    await conn.commit()


def _collection(coll_id: str, *, system: bool = False) -> Collection:
    return Collection(
        id=coll_id,
        description="test",
        embedder=CollectionEmbedder(provider_id="emb", model="m"),
        search_provider_id="ssp",
        system=system,
    )


@pytest_asyncio.fixture
async def provider(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "t.sqlite"),
    )
    p = StorageProviderFactory.create(cfg)
    await p.initialize()
    await p.get_content_store().ensure_schema()
    yield p
    await p.aclose()


async def _path_of(provider, doc_id: str) -> str:
    docs = provider.get_storage(Document)
    doc = await docs.get(doc_id)
    return doc.path


async def test_migrate_legacy_bodies_and_paths(provider):
    colls = provider.get_storage(Collection)
    await colls.create(_collection("c1"))
    await colls.create(_collection("sys1", system=True))

    await _raw_insert_document(
        provider,
        {"id": "d1", "collection_id": "c1", "name": "Runbook", "meta": {"content": "BODYC"}},
    )
    await _raw_insert_document(
        provider,
        {"id": "d2", "collection_id": "c1", "name": "Notes", "meta": {"text": "BODYT"}},
    )
    await _raw_insert_document(
        provider,
        {"id": "d3", "collection_id": "c1", "name": "Empty", "meta": {}},
    )
    await _raw_insert_document(
        provider,
        {"id": "s1", "collection_id": "sys1", "name": "cat", "meta": {"content": "X"}},
    )

    migrated = await migrate_document_content(provider)
    assert migrated == 3

    cs = provider.get_content_store()
    assert await cs.get("d1") == "BODYC"
    assert await cs.get("d2") == "BODYT"
    assert await cs.get("d3") == ""  # no body -> empty content row still created

    # each migrated doc now has a unique path + title; resolve by path works
    assert await cs.resolve_id("c1", (await _path_of(provider, "d1"))) == "d1"

    # the upgraded entity row now loads cleanly via Storage[Document] (has a path)
    docs = provider.get_storage(Document)
    d1 = await docs.get("d1")
    assert d1.path  # non-empty
    assert d1.title == "Runbook"

    # system collection doc is UNTOUCHED (no content row)
    assert await cs.get("s1") is None

    # idempotent: running again is a no-op (no duplicate/conflict)
    again = await migrate_document_content(provider)
    assert again == 0
    assert await cs.get("d1") == "BODYC"


async def test_path_collision_yields_distinct_paths(provider):
    colls = provider.get_storage(Collection)
    await colls.create(_collection("c1"))

    await _raw_insert_document(
        provider,
        {"id": "a1", "collection_id": "c1", "name": "Runbook", "meta": {"content": "A"}},
    )
    await _raw_insert_document(
        provider,
        {"id": "a2", "collection_id": "c1", "name": "Runbook", "meta": {"content": "B"}},
    )

    migrated = await migrate_document_content(provider)
    assert migrated == 2

    p1 = await _path_of(provider, "a1")
    p2 = await _path_of(provider, "a2")
    assert p1 != p2  # distinct paths despite identical names

    cs = provider.get_content_store()
    assert await cs.resolve_id("c1", p1) == "a1"
    assert await cs.resolve_id("c1", p2) == "a2"
