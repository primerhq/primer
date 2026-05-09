"""Unit tests for matrix.api.deps — singleton resolvers + principal passthrough."""

from __future__ import annotations

from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport

from matrix.api.deps import (
    PRINCIPAL_HEADER,
    get_cross_encoder_provider_storage,
    get_embedding_provider_storage,
    get_llm_provider_storage,
    get_principal,
    get_provider_registry,
    get_storage_provider,
    get_toolset_storage,
    get_vector_store_registry,
)
from matrix.api.errors import register_error_handlers
from matrix.api.registries import ProviderRegistry, VectorStoreRegistry


def _mount_state_echo(app: FastAPI) -> None:
    @app.get("/echo-state")
    def _echo(
        sp=Depends(get_storage_provider),
        pr=Depends(get_provider_registry),
        vsr=Depends(get_vector_store_registry),
    ) -> dict:
        return {
            "storage_provider": sp is not None,
            "provider_registry": isinstance(pr, ProviderRegistry),
            "vector_store_registry": isinstance(vsr, VectorStoreRegistry),
        }

    @app.get("/echo-principal")
    def _echo_principal(p: Annotated[str | None, Depends(get_principal)]) -> dict:
        return {"principal": p}


@pytest.mark.asyncio
async def test_singleton_resolvers_return_app_state(client, app) -> None:
    _mount_state_echo(app)
    response = await client.get("/echo-state")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "storage_provider": True,
        "provider_registry": True,
        "vector_store_registry": True,
    }


@pytest.mark.asyncio
async def test_principal_header_round_trips(client, app) -> None:
    _mount_state_echo(app)
    response = await client.get(
        "/echo-principal",
        headers={PRINCIPAL_HEADER: "alice@example.com"},
    )
    assert response.status_code == 200
    assert response.json() == {"principal": "alice@example.com"}


@pytest.mark.asyncio
async def test_principal_absent_when_header_missing(client, app) -> None:
    _mount_state_echo(app)
    response = await client.get("/echo-principal")
    assert response.status_code == 200
    assert response.json() == {"principal": None}


@pytest.mark.asyncio
async def test_uninitialised_app_state_returns_500_problem() -> None:
    """An app missing app.state attributes returns 500 ProblemDetails."""
    bare = FastAPI()
    register_error_handlers(bare)
    _mount_state_echo(bare)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=bare), base_url="http://test"
    ) as c:
        response = await c.get("/echo-state")
    assert response.status_code == 500
    body = response.json()
    assert body["type"] == "/errors/misconfigured"
    assert "API state not initialised" in body["detail"]


@pytest.mark.asyncio
async def test_per_model_storage_helpers_resolve_correct_handles(
    client, app, fake_storage_provider
) -> None:
    """The four Storage[T] helpers each return the right typed handle."""
    from matrix.model.provider import (
        CrossEncoderProvider,
        EmbeddingProvider,
        LLMProvider,
        Toolset,
    )

    @app.get("/echo-storages")
    def _echo(
        llm=Depends(get_llm_provider_storage),
        emb=Depends(get_embedding_provider_storage),
        ce=Depends(get_cross_encoder_provider_storage),
        ts=Depends(get_toolset_storage),
    ) -> dict:
        # Every fake storage handle is the same instance returned by
        # _FakeStorageProvider.get_storage(model_class); proving identity
        # against the same call from the test exercises the dependency
        # path end-to-end.
        return {
            "llm": llm is fake_storage_provider.get_storage(LLMProvider),
            "emb": emb is fake_storage_provider.get_storage(EmbeddingProvider),
            "ce": ce is fake_storage_provider.get_storage(CrossEncoderProvider),
            "ts": ts is fake_storage_provider.get_storage(Toolset),
        }

    response = await client.get("/echo-storages")
    assert response.status_code == 200
    assert response.json() == {"llm": True, "emb": True, "ce": True, "ts": True}
