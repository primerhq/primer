"""Tests for CRUD locks on harness-managed entities.

Each of the five managed entity types (Agent, Graph, Collection, Document,
Toolset) must:
  - Reject PUT with 409 (managed_entity) when harness_id is set.
  - Reject DELETE with 409 (managed_entity) when harness_id is set.
  - Allow GET (read is free).
  - Reject POST with 422 (managed_field_set) when body sets harness_id.
"""

from __future__ import annotations

import pytest

from primer.model.agent import Agent, AgentModel
from primer.model.collection import Collection, CollectionEmbedder, Document
from primer.model.graph import (
    Graph,
    _AgentNodeRef,
    _BeginNode,
    _EndNode,
    _StaticEdge,
)
from primer.model.provider import Toolset, ToolsetProviderType


# ---------------------------------------------------------------------------
# Minimal builders — each returns a model instance with harness_id="h-test"
# ---------------------------------------------------------------------------


def _build_agent(harness_id: str | None = "h-test") -> Agent:
    return Agent(
        id="managed-agent-1",
        description="a managed agent",
        model=AgentModel(provider_id="llm-1", model_name="gpt-4o"),
        harness_id=harness_id,
    )


def _build_graph(harness_id: str | None = "h-test") -> Graph:
    return Graph(
        id="managed-graph-1",
        description="a managed graph",
        nodes=[
            _BeginNode(id="begin"),
            _AgentNodeRef(id="n1", agent_id="agt-1"),
            _EndNode(id="end"),
        ],
        edges=[
            _StaticEdge(from_node="begin", to_node="n1"),
            _StaticEdge(from_node="n1", to_node="end"),
        ],
        harness_id=harness_id,
    )


def _build_collection(harness_id: str | None = "h-test") -> Collection:
    return Collection(
        id="managed-coll-1",
        description="a managed collection",
        embedder=CollectionEmbedder(provider_id="emb-1", model="text-embed-3"),
        search_provider_id="ssp-1",
        harness_id=harness_id,
    )


def _build_document(harness_id: str | None = "h-test") -> Document:
    return Document(
        id="managed-doc-1",
        collection_id="coll-1",
        name="managed doc",
        path="managed-doc-1.md",
        harness_id=harness_id,
    )


def _build_toolset(harness_id: str | None = "h-test") -> Toolset:
    return Toolset(
        id="managed-toolset-1",
        provider=ToolsetProviderType.INTERNAL,
        harness_id=harness_id,
    )


# ---------------------------------------------------------------------------
# Valid PUT bodies for each entity (same id, no harness_id so it's a clean
# replacement body — the harness_id guard fires on the *stored* row).
# ---------------------------------------------------------------------------


def _agent_put_body() -> dict:
    return _build_agent(harness_id=None).model_dump(mode="json")


def _graph_put_body() -> dict:
    return _build_graph(harness_id=None).model_dump(mode="json")


def _collection_put_body() -> dict:
    # Must keep the same search_provider_id to pass the immutability check
    # (which runs AFTER the harness guard — so we never reach it).
    return _build_collection(harness_id=None).model_dump(mode="json")


def _document_put_body() -> dict:
    return _build_document(harness_id=None).model_dump(mode="json")


def _toolset_put_body() -> dict:
    return _build_toolset(harness_id=None).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Parametrize
# ---------------------------------------------------------------------------

_CASES = [
    (
        "agent",
        "agents",
        Agent,
        _build_agent,
        lambda: _build_agent(harness_id=None).model_dump(mode="json"),
    ),
    (
        "graph",
        "graphs",
        Graph,
        _build_graph,
        lambda: _build_graph(harness_id=None).model_dump(mode="json"),
    ),
    (
        "collection",
        "collections",
        Collection,
        _build_collection,
        lambda: _build_collection(harness_id=None).model_dump(mode="json"),
    ),
    (
        "document",
        "documents",
        Document,
        _build_document,
        lambda: _build_document(harness_id=None).model_dump(mode="json"),
    ),
    (
        "toolset",
        "toolsets",
        Toolset,
        _build_toolset,
        lambda: _build_toolset(harness_id=None).model_dump(mode="json"),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,plural,model_cls,builder,put_body_fn", _CASES)
async def test_put_rejects_managed_entity_with_409(
    client, fake_storage_provider, kind, plural, model_cls, builder, put_body_fn
):
    """PUT on a managed row returns 409 managed_entity."""
    row = builder()
    await fake_storage_provider.get_storage(model_cls).create(row)

    body = put_body_fn()
    resp = await client.put(f"/v1/{plural}/{row.id}", json=body)
    assert resp.status_code == 409, f"{kind} PUT: {resp.text}"
    ext = resp.json()["extensions"]
    assert ext["code"] == "managed_entity", ext
    assert ext["field"] == "harness_id", ext


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,plural,model_cls,builder,put_body_fn", _CASES)
async def test_delete_rejects_managed_entity_with_409(
    client, fake_storage_provider, kind, plural, model_cls, builder, put_body_fn
):
    """DELETE on a managed row returns 409 managed_entity."""
    row = builder()
    await fake_storage_provider.get_storage(model_cls).create(row)

    resp = await client.delete(f"/v1/{plural}/{row.id}")
    assert resp.status_code == 409, f"{kind} DELETE: {resp.text}"
    ext = resp.json()["extensions"]
    assert ext["code"] == "managed_entity", ext
    assert ext["field"] == "harness_id", ext


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,plural,model_cls,builder,put_body_fn", _CASES)
async def test_get_allows_managed_entity(
    client, fake_storage_provider, kind, plural, model_cls, builder, put_body_fn
):
    """GET on a managed row returns 200 — read is always free."""
    row = builder()
    await fake_storage_provider.get_storage(model_cls).create(row)

    resp = await client.get(f"/v1/{plural}/{row.id}")
    assert resp.status_code == 200, f"{kind} GET: {resp.text}"
    assert resp.json()["id"] == row.id


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,plural,model_cls,builder,put_body_fn", _CASES)
async def test_post_rejects_harness_id_in_body_with_422(
    client, kind, plural, model_cls, builder, put_body_fn
):
    """POST with harness_id set in the body returns 422 managed_field_set."""
    body = builder(harness_id="x").model_dump(mode="json")
    # Use a fresh id to avoid conflicts with other tests
    body["id"] = f"new-{kind}-99"
    resp = await client.post(f"/v1/{plural}", json=body)
    assert resp.status_code == 422, f"{kind} POST: {resp.text}"
    ext = resp.json()["extensions"]
    assert ext["error"] == "managed_field_set", ext
    assert ext["field"] == "harness_id", ext


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,plural,model_cls,builder,put_body_fn", _CASES)
async def test_put_allows_unmanaged_entity(
    client, fake_storage_provider, kind, plural, model_cls, builder, put_body_fn
):
    """PUT on a row without harness_id succeeds (non-managed entities are free)."""
    row = builder(harness_id=None)
    await fake_storage_provider.get_storage(model_cls).create(row)

    body = put_body_fn()
    resp = await client.put(f"/v1/{plural}/{row.id}", json=body)
    # 200 OK on success
    assert resp.status_code == 200, f"{kind} PUT unmanaged: {resp.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,plural,model_cls,builder,put_body_fn", _CASES)
async def test_delete_allows_unmanaged_entity(
    client, fake_storage_provider, kind, plural, model_cls, builder, put_body_fn
):
    """DELETE on a row without harness_id succeeds (non-managed entities are free)."""
    row = builder(harness_id=None)
    await fake_storage_provider.get_storage(model_cls).create(row)

    resp = await client.delete(f"/v1/{plural}/{row.id}")
    assert resp.status_code == 204, f"{kind} DELETE unmanaged: {resp.text}"
