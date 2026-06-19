"""Route tests for the path-addressed document API (Task 11).

These exercise GET/PUT/DELETE/list/move on
``/v1/collections/{cid}/documents`` over a REAL sqlite provider, because
the path-addressed routes delegate to
:class:`primer.knowledge.document_service.DocumentService`, which writes
the entity row and the content-store body row inside ONE backend
transaction. The shared ``_FakeStorageProvider`` has no ``transaction()``
or real content store, so this module builds its own sqlite-backed app
fixture instead of reusing the package ``client`` fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.model.collection import Collection, CollectionEmbedder
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.storage.factory import StorageProviderFactory


_SSP_BODY = {
    "id": "ssp-test",
    "provider": "pgvector",
    "config": {
        "hostname": "localhost",
        "port": 5432,
        "database": "primer",
        "username": "primer",
        "password": "primer",
        "db_schema": "public",
    },
}


@pytest_asyncio.fixture
async def provider(tmp_path):
    cfg = StorageProviderConfig(
        provider=StorageProviderType.SQLITE,
        config=SqliteConfig(path=tmp_path / "knowledge.sqlite"),
    )
    sp = StorageProviderFactory.create(cfg)
    await sp.initialize()
    await sp.get_content_store().ensure_schema()
    yield sp
    await sp.aclose()


@pytest_asyncio.fixture
async def app(provider):
    registry = ProviderRegistry(
        provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )
    _app = create_test_app(storage_provider=provider, provider_registry=registry)
    if getattr(_app.state, "seed_artifact_default", None) is not None:
        await _app.state.seed_artifact_default()
    yield _app


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:
            pass
        yield c


@pytest_asyncio.fixture
async def collection_id(client) -> str:
    await client.post("/v1/ssp", json=_SSP_BODY)
    body = Collection(
        id="kb-1",
        description="test collection",
        embedder=CollectionEmbedder(provider_id="hf-1", model="all-MiniLM-L6-v2"),
        search_provider_id="ssp-test",
    ).model_dump(mode="json")
    created = await client.post("/v1/collections", json=body)
    assert created.status_code == 201, created.text
    return "kb-1"


def _docs_url(cid: str) -> str:
    return f"/v1/collections/{cid}/documents"


@pytest.mark.asyncio
async def test_put_then_get_by_path(client, collection_id):
    r = await client.put(
        _docs_url(collection_id),
        params={"path": "a/b.md"},
        json={"content": "hello", "title": "B"},
    )
    assert r.status_code in (200, 201), r.text
    g = await client.get(_docs_url(collection_id), params={"path": "a/b.md"})
    assert g.status_code == 200, g.text
    assert g.json()["content"] == "hello"


@pytest.mark.asyncio
async def test_list_by_prefix_has_no_bodies(client, collection_id):
    await client.put(
        _docs_url(collection_id), params={"path": "docs/a.md"}, json={"content": "x"}
    )
    await client.put(
        _docs_url(collection_id), params={"path": "docs/b.md"}, json={"content": "y"}
    )
    r = await client.get(_docs_url(collection_id), params={"prefix": "docs/"})
    assert r.status_code == 200, r.text
    items = r.json()["documents"]
    assert {i["path"] for i in items} == {"docs/a.md", "docs/b.md"}
    assert all("content" not in i for i in items)


@pytest.mark.asyncio
async def test_move(client, collection_id):
    await client.put(
        _docs_url(collection_id), params={"path": "a.md"}, json={"content": "x"}
    )
    m = await client.post(
        f"/v1/collections/{collection_id}/documents/move",
        json={"from": "a.md", "to": "c.md"},
    )
    assert m.status_code in (200, 204), m.text
    gone = await client.get(_docs_url(collection_id), params={"path": "a.md"})
    assert gone.status_code == 404
    moved = await client.get(_docs_url(collection_id), params={"path": "c.md"})
    assert moved.status_code == 200


@pytest.mark.asyncio
async def test_get_missing_is_404_problemdetails(client, collection_id):
    r = await client.get(_docs_url(collection_id), params={"path": "nope.md"})
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_put_conflicting_path_is_409(client, collection_id):
    # An upsert to the same path is an update, not a conflict. A conflict
    # arises via a move onto an occupied path.
    await client.put(
        _docs_url(collection_id), params={"path": "a.md"}, json={"content": "x"}
    )
    await client.put(
        _docs_url(collection_id), params={"path": "b.md"}, json={"content": "y"}
    )
    m = await client.post(
        f"/v1/collections/{collection_id}/documents/move",
        json={"from": "b.md", "to": "a.md"},
    )
    assert m.status_code == 409, m.text


@pytest.mark.asyncio
async def test_delete(client, collection_id):
    await client.put(
        _docs_url(collection_id), params={"path": "a.md"}, json={"content": "x"}
    )
    d = await client.delete(_docs_url(collection_id), params={"path": "a.md"})
    assert d.status_code in (200, 204), d.text
    gone = await client.get(_docs_url(collection_id), params={"path": "a.md"})
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_move_missing_src_is_404(client, collection_id):
    m = await client.post(
        f"/v1/collections/{collection_id}/documents/move",
        json={"from": "missing.md", "to": "c.md"},
    )
    assert m.status_code == 404, m.text


@pytest.mark.asyncio
async def test_put_indexes_when_search_on(app, client, collection_id):
    """Behaviour-preserving: a path-addressed PUT must still index the
    document body when search is on. Stub the collection's embedder + the
    vector store so the write triggers an indexing pass, then assert the
    stub store received chunks read from the content store."""
    from tests.knowledge.test_indexing import _Emb, _Store

    store = _Store()
    # Patch the registry + SSR resolved by the indexer so the PUT's
    # indexing pass uses our in-memory stubs.
    app.state.provider_registry.get_embedder = AsyncMock(return_value=_Emb(dim=4))
    app.state.semantic_search_registry.get_store = AsyncMock(return_value=store)

    r = await client.put(
        _docs_url(collection_id),
        params={"path": "indexed.md"},
        json={"content": "alpha beta gamma"},
    )
    assert r.status_code in (200, 201), r.text

    # The body was chunked + embedded + put into the stub store.
    assert store.puts, "PUT did not index the document body"
    assert all(p.collection_id == collection_id for p in store.puts)
    recombined = "\n\n".join(p.text for p in store.puts)
    assert recombined == "alpha beta gamma"
