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
    forwarder = await _app.state.start_chat_tick_forwarder()

    # Start the worker pool if the app was built with one.
    if getattr(_app.state, "start_worker_pool", None) is not None:
        await _app.state.start_worker_pool()

    try:
        yield _app
    finally:
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
