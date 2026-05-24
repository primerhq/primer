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
    get_scheduler,
    get_session_storage,
    get_storage_provider,
    get_toolset_storage,
    get_worker_pool,
)
from matrix.api.errors import register_error_handlers
from matrix.api.registries import ProviderRegistry
from matrix.model.except_ import ConfigError
from matrix.model.session import Session


def _mount_state_echo(app: FastAPI) -> None:
    @app.get("/echo-state")
    def _echo(
        sp=Depends(get_storage_provider),
        pr=Depends(get_provider_registry),
    ) -> dict:
        return {
            "storage_provider": sp is not None,
            "provider_registry": isinstance(pr, ProviderRegistry),
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
async def test_uninitialised_app_state_returns_503_problem() -> None:
    """An app missing app.state attributes raises ConfigError, which maps to 503."""
    bare = FastAPI()
    register_error_handlers(bare)
    _mount_state_echo(bare)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=bare), base_url="http://test"
    ) as c:
        response = await c.get("/echo-state")
    assert response.status_code == 503
    body = response.json()
    assert body["type"] == "/errors/service-unavailable"
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


# --- Background-execution scheduler/worker helpers (Task 18) ----------------


def _fake_request(app: FastAPI):
    """Build a minimal :class:`Request` whose ``.app`` points at ``app``."""
    from fastapi import Request

    scope = {
        "type": "http",
        "app": app,
        "headers": [],
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


def test_get_session_storage_returns_storage(app, fake_storage_provider):
    """`get_session_storage` resolves to the Session-typed handle."""
    helper_storage = get_session_storage(sp=fake_storage_provider)
    assert helper_storage is fake_storage_provider.get_storage(Session)


def test_get_scheduler_returns_scheduler_when_present(app):
    from matrix.scheduler.in_memory import InMemoryScheduler

    sched = InMemoryScheduler()
    app.state.scheduler = sched
    req = _fake_request(app)
    assert get_scheduler(req) is sched


def test_get_scheduler_raises_when_missing(app):
    app.state.scheduler = None
    req = _fake_request(app)
    with pytest.raises(ConfigError):
        get_scheduler(req)


def test_get_worker_pool_returns_pool_when_present(app):
    sentinel = object()
    app.state.worker_pool = sentinel
    req = _fake_request(app)
    assert get_worker_pool(req) is sentinel


def test_get_worker_pool_raises_when_missing(app):
    app.state.worker_pool = None
    req = _fake_request(app)
    with pytest.raises(ConfigError):
        get_worker_pool(req)


def test_get_worker_pool_raises_when_attribute_absent(app):
    # Defensive: helper should also raise if attribute was never set
    if hasattr(app.state, "worker_pool"):
        delattr(app.state, "worker_pool")
    req = _fake_request(app)
    with pytest.raises(ConfigError):
        get_worker_pool(req)


def test_get_scheduler_raises_when_attribute_absent(app):
    if hasattr(app.state, "scheduler"):
        delattr(app.state, "scheduler")
    req = _fake_request(app)
    with pytest.raises(ConfigError):
        get_scheduler(req)
