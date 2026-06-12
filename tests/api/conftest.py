"""Shared fixtures for the FastAPI test suite."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry

# Re-export the shared storage helpers so existing imports from this
# module continue to work.
from tests.conftest import (  # noqa: F401
    _FakeStorageProvider,
    _InMemoryStorage,
)


@pytest.fixture
def fake_provider_registry(
    fake_storage_provider: _FakeStorageProvider,
) -> ProviderRegistry:
    return ProviderRegistry(
        fake_storage_provider,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),  # type: ignore[arg-type]
        embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(),  # type: ignore[arg-type]
        toolset_factory=lambda p: object(),  # type: ignore[arg-type]
    )


@pytest_asyncio.fixture
async def app(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
) -> AsyncIterator[FastAPI]:
    _app = create_test_app(
        storage_provider=fake_storage_provider,  # type: ignore[arg-type]
        provider_registry=fake_provider_registry,
    )
    # Seed the reserved default artifact provider (parity with the lifespan).
    if getattr(_app.state, "seed_artifact_default", None) is not None:
        await _app.state.seed_artifact_default()

    # Bootstrap web-search reserved rows (DDG provider + active config
    # singleton) at test startup. Matches the production lifespan.
    from primer.api.app import _bootstrap_web_search
    await _bootstrap_web_search(fake_storage_provider)

    # Construct the web-search registry + service from the bootstrapped rows.
    from primer.api.registries.web_search_registry import (
        WebSearchRegistry,
        default_web_search_factory,
    )
    from primer.model.web_search import (
        ActiveWebSearchConfig,
        WebSearchProvider,
    )
    from primer.web_search.service import WebSearchService

    ws_registry = WebSearchRegistry(
        storage=fake_storage_provider.get_storage(WebSearchProvider),
        factory=default_web_search_factory,
    )
    ws_service = WebSearchService(
        registry=ws_registry,
        active_config_storage=fake_storage_provider.get_storage(
            ActiveWebSearchConfig
        ),
    )
    _app.state.web_search_registry = ws_registry
    _app.state.web_search_service = ws_service

    # Bootstrap web-fetch reserved rows (LOCAL provider + active config
    # singleton) at test startup. Matches the production lifespan.
    from primer.api.app import _bootstrap_web_fetch
    await _bootstrap_web_fetch(fake_storage_provider)

    # Construct the web-fetch registry + service from the bootstrapped rows.
    from primer.api.registries.web_fetch_registry import (
        WebFetchRegistry,
        default_web_fetch_factory,
    )
    from primer.model.web_fetch import (
        ActiveWebFetchConfig,
        WebFetchProvider,
    )
    from primer.web_fetch.service import WebFetchService

    wf_registry = WebFetchRegistry(
        storage=fake_storage_provider.get_storage(WebFetchProvider),
        factory=default_web_fetch_factory,
    )
    wf_service = WebFetchService(
        registry=wf_registry,
        active_config_storage=fake_storage_provider.get_storage(
            ActiveWebFetchConfig
        ),
    )
    _app.state.web_fetch_registry = wf_registry
    _app.state.web_fetch_service = wf_service

    forwarder = await _app.state.start_chat_tick_forwarder()

    # Start the worker pool if the app was built with one.
    if getattr(_app.state, "start_worker_pool", None) is not None:
        await _app.state.start_worker_pool()

    # Start the MCP /v1/mcp mount so tests that hit the endpoint
    # see the real auth gate + session manager surface.
    if getattr(_app.state, "start_mcp_mount", None) is not None:
        await _app.state.start_mcp_mount()

    try:
        yield _app
    finally:
        # Stop the MCP mount first — its anyio task group depends on
        # the asyncio loop being alive.
        if getattr(_app.state, "stop_mcp_mount", None) is not None:
            try:
                await _app.state.stop_mcp_mount()
            except Exception:
                pass
        # Stop the worker pool before cancelling the forwarder.
        if getattr(_app.state, "stop_worker_pool", None) is not None:
            try:
                await _app.state.stop_worker_pool()
            except Exception:
                pass
        forwarder.cancel()
        try:
            await forwarder
        except asyncio.CancelledError:
            pass


@pytest.fixture
async def raw_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Plain HTTP client with no auto-authentication.

    Use this from auth-focused tests that need to exercise the
    unauthenticated and first-boot code paths.
    """
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client with an auto-registered + logged-in test user.

    Almost every existing test pre-dates auth; rather than make each
    one log in, the fixture registers a default user and yields a
    client that already carries the signed session cookie. Auth-
    focused tests should depend on ``raw_client`` instead.
    """
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        # Best-effort register; ignore 409 if a prior test already
        # created the user (per-test storage reset means this is
        # almost always the first call).
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:
            pass
        yield c


__all__ = [
    "_FakeStorageProvider",
    "_InMemoryStorage",
]
