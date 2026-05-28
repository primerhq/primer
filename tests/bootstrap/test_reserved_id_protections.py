"""Tests for reserved-id 409/403 protections on provider CRUD endpoints.

Covers all five router kinds:
* EmbeddingProvider    POST /v1/embedding_providers  → 409 (id='huggingface')
* EmbeddingProvider    DELETE /v1/embedding_providers/<id> → 403
* CrossEncoderProvider POST /v1/cross_encoder_providers → 409 (id='huggingface-ce')
* CrossEncoderProvider DELETE /v1/cross_encoder_providers/<id> → 403
* SemanticSearchProvider POST /v1/ssp → 409 (id='lance')
* SemanticSearchProvider DELETE /v1/ssp/<id> → 403
* WorkspaceProvider    POST /v1/workspace_providers → 409 (id='local')
* WorkspaceProvider    DELETE /v1/workspace_providers/<id> → 403
* LLMProvider          POST non-reserved id → 201  (no reserved LLM ids)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from tests.conftest import _FakeStorageProvider


# ---------------------------------------------------------------------------
# Shared app fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def storage() -> _FakeStorageProvider:
    return _FakeStorageProvider()


@pytest.fixture
def registry(storage: _FakeStorageProvider) -> ProviderRegistry:
    return ProviderRegistry(
        storage,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda t: object(),  # type: ignore[arg-type]
    )


@pytest_asyncio.fixture
async def app(
    storage: _FakeStorageProvider,
    registry: ProviderRegistry,
) -> AsyncIterator[FastAPI]:
    _app = create_test_app(
        storage_provider=storage,  # type: ignore[arg-type]
        provider_registry=registry,
    )
    forwarder = await _app.state.start_chat_tick_forwarder()
    try:
        yield _app
    finally:
        forwarder.cancel()
        try:
            await forwarder
        except asyncio.CancelledError:
            pass


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
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


# ---------------------------------------------------------------------------
# Minimal valid request bodies for each protected kind
# ---------------------------------------------------------------------------


def _embedding_body(provider_id: str) -> dict:
    """Minimal valid EmbeddingProvider body."""
    return {
        "id": provider_id,
        "provider": "huggingface",
        "models": [{"name": "BAAI/bge-small-en-v1.5"}],
        "config": {"token": ""},
        "limits": {"max_concurrency": 2},
    }


def _cross_encoder_body(provider_id: str) -> dict:
    """Minimal valid CrossEncoderProvider body."""
    return {
        "id": provider_id,
        "provider": "huggingface",
        "models": [{"name": "cross-encoder/ms-marco-MiniLM-L-6-v2", "max_pair_length": None}],
        "config": {"token": None},
        "limits": {"max_concurrency": 2},
    }


def _ssp_body(provider_id: str) -> dict:
    """Minimal valid SemanticSearchProvider body (pgvector to avoid path issues)."""
    return {
        "id": provider_id,
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


def _workspace_provider_body(provider_id: str) -> dict:
    """Minimal valid WorkspaceProvider body."""
    return {
        "id": provider_id,
        "provider": "local",
        "config": {"kind": "local", "path": "/tmp/primer-test-ws"},
    }


def _llm_body(provider_id: str) -> dict:
    """Minimal valid LLMProvider body."""
    return {
        "id": provider_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200000}],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }


# ---------------------------------------------------------------------------
# EmbeddingProvider protection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_embedding_provider_with_reserved_id_returns_409(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/embedding_providers", json=_embedding_body("huggingface")
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "reserved_id"
    assert "huggingface" in detail["reserved"]


@pytest.mark.asyncio
async def test_delete_reserved_embedding_provider_returns_403(
    client: httpx.AsyncClient,
) -> None:
    # Protection fires BEFORE the storage lookup — no pre-seeding needed.
    resp = await client.delete("/v1/embedding_providers/huggingface")
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "reserved_id_protected"


@pytest.mark.asyncio
async def test_post_non_reserved_embedding_provider_succeeds(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/embedding_providers", json=_embedding_body("my-embedder")
    )
    assert resp.status_code == 201, resp.text
    await client.delete("/v1/embedding_providers/my-embedder")


# ---------------------------------------------------------------------------
# CrossEncoderProvider protection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_cross_encoder_provider_with_reserved_id_returns_409(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/cross_encoder_providers", json=_cross_encoder_body("huggingface-ce")
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "reserved_id"
    assert "huggingface-ce" in detail["reserved"]


@pytest.mark.asyncio
async def test_delete_reserved_cross_encoder_provider_returns_403(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.delete("/v1/cross_encoder_providers/huggingface-ce")
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "reserved_id_protected"


@pytest.mark.asyncio
async def test_post_non_reserved_cross_encoder_provider_succeeds(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/cross_encoder_providers", json=_cross_encoder_body("my-ce")
    )
    assert resp.status_code == 201, resp.text
    await client.delete("/v1/cross_encoder_providers/my-ce")


# ---------------------------------------------------------------------------
# SemanticSearchProvider (SSP) protection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_ssp_with_reserved_id_returns_409(
    client: httpx.AsyncClient,
) -> None:
    # "lance" reserved id: use a pgvector body (same endpoint, only id differs).
    resp = await client.post("/v1/ssp", json=_ssp_body("lance"))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "reserved_id"
    assert "lance" in detail["reserved"]


@pytest.mark.asyncio
async def test_delete_reserved_ssp_returns_403(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.delete("/v1/ssp/lance")
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "reserved_id_protected"


@pytest.mark.asyncio
async def test_post_non_reserved_ssp_succeeds(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post("/v1/ssp", json=_ssp_body("my-ssp"))
    assert resp.status_code == 201, resp.text
    await client.delete("/v1/ssp/my-ssp")


# ---------------------------------------------------------------------------
# WorkspaceProvider protection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_workspace_provider_with_reserved_id_returns_409(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/workspace_providers", json=_workspace_provider_body("local")
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "reserved_id"
    assert "local" in detail["reserved"]


@pytest.mark.asyncio
async def test_delete_reserved_workspace_provider_returns_403(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.delete("/v1/workspace_providers/local")
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "reserved_id_protected"


@pytest.mark.asyncio
async def test_post_non_reserved_workspace_provider_succeeds(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/workspace_providers", json=_workspace_provider_body("my-wp")
    )
    assert resp.status_code == 201, resp.text
    await client.delete("/v1/workspace_providers/my-wp")


# ---------------------------------------------------------------------------
# LLMProvider: no reserved ids — normal POST should succeed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_llm_provider_is_not_blocked(
    client: httpx.AsyncClient,
) -> None:
    """LLM providers have no reserved ids; any operator-chosen id is allowed."""
    resp = await client.post(
        "/v1/llm_providers", json=_llm_body("anthropic-1")
    )
    assert resp.status_code == 201, resp.text
    await client.delete("/v1/llm_providers/anthropic-1")
