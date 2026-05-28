"""Tests for the /v1/_test/* instrumentation endpoints.

These endpoints are only mounted when ``MATRIX_ENABLE_TEST_ENDPOINTS=1``
and must return 404 when that env var is absent.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport

from matrix.api.app import create_test_app
from matrix.api.registries import ProviderRegistry
from matrix.coordinator.in_memory import InMemoryRateLimiter
from matrix.int.coordinator import Coordinator, InvalidationBus, LeaderElector
from tests.conftest import _FakeStorageProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_with_env(
    *,
    storage_provider: _FakeStorageProvider,
    provider_registry: ProviderRegistry,
    enable_test_endpoints: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """Build a test app with the env var optionally set."""
    if enable_test_endpoints:
        monkeypatch.setenv("MATRIX_ENABLE_TEST_ENDPOINTS", "1")
    else:
        monkeypatch.delenv("MATRIX_ENABLE_TEST_ENDPOINTS", raising=False)

    app = create_test_app(
        storage_provider=storage_provider,  # type: ignore[arg-type]
        provider_registry=provider_registry,
    )
    return app


def _attach_coordinator(app: FastAPI) -> InMemoryRateLimiter:
    """Attach a minimal Coordinator on app.state and return the rate limiter."""
    rate_limiter = InMemoryRateLimiter()
    # Minimal stubs for the other two coordinator fields.
    invalidation_bus = MagicMock(spec=InvalidationBus)
    leader_elector = MagicMock(spec=LeaderElector)
    app.state.coordinator = Coordinator(
        rate_limiter=rate_limiter,
        invalidation_bus=invalidation_bus,
        leader_elector=leader_elector,
    )
    return rate_limiter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_storage_provider() -> _FakeStorageProvider:
    return _FakeStorageProvider()


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_rate_limit_returns_200_when_env_set(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When MATRIX_ENABLE_TEST_ENDPOINTS=1, the endpoint mounts and returns 200."""
    app = _make_app_with_env(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        enable_test_endpoints=True,
        monkeypatch=monkeypatch,
    )
    _attach_coordinator(app)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/_test/acquire_rate_limit",
            params={"key": "test-key", "max_concurrency": 3, "sleep_ms": 0},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_acquire_rate_limit_returns_404_when_env_unset(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When MATRIX_ENABLE_TEST_ENDPOINTS is unset, the endpoint returns 404."""
    app = _make_app_with_env(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        enable_test_endpoints=False,
        monkeypatch=monkeypatch,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/_test/acquire_rate_limit",
            params={"key": "test-key", "max_concurrency": 3, "sleep_ms": 0},
        )

    assert resp.status_code == 404
