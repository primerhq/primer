"""Tests for the /v1/_test/* instrumentation endpoints.

These endpoints are only mounted when ``PRIMER_ENABLE_TEST_ENDPOINTS=1``
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

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.coordinator.in_memory import InMemoryRateLimiter
from primer.int.coordinator import Coordinator, InvalidationBus, LeaderElector
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
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
        monkeypatch.setenv("PRIMER_ENABLE_TEST_ENDPOINTS", "1")
    else:
        monkeypatch.delenv("PRIMER_ENABLE_TEST_ENDPOINTS", raising=False)

    app = create_test_app(
        storage_provider=storage_provider,  # type: ignore[arg-type]
        provider_registry=provider_registry,
    )
    return app


async def _seed_session(
    storage_provider: _FakeStorageProvider, *, sid: str = "sess-park-test",
) -> WorkspaceSession:
    from datetime import datetime, timezone

    row = WorkspaceSession(
        id=sid,
        workspace_id="ws-park-test",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.CREATED,
        created_at=datetime(2026, 8, 16, 10, 0, 0, tzinfo=timezone.utc),
    )
    await storage_provider.get_storage(WorkspaceSession).create(row)
    return row


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
    """When PRIMER_ENABLE_TEST_ENDPOINTS=1, the endpoint mounts and returns 200."""
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
    """When PRIMER_ENABLE_TEST_ENDPOINTS is unset, the endpoint returns 404."""
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


# ---------------------------------------------------------------------------
# US-011e — deterministic park injection
#
# Replaces the raw asyncpg jsonb_set injection tests/ui_e2e/
# test_approvals_journey.py's _inject_approval_park used to reach a
# parked session: this endpoint writes through the real WorkspaceSession
# model + storage layer instead, so a parked_state shape drift fails
# the call immediately (422) instead of a hand-rolled SQL chain silently
# drifting from production.
# ---------------------------------------------------------------------------


def _park_body(*, event_key: str = "ask_user:sess-park-test:tc-1") -> dict:
    return {
        "parked_state": {
            "tool_call_id": "tc-1",
            "yielded": {
                "tool_name": "ask_user",
                "event_key": event_key,
                "resume_metadata": {"prompt": "continue?", "tool_call_id": "tc-1"},
            },
            "llm_messages": [],
            "turn_no": 0,
        },
        "parked_event_key": event_key,
    }


@pytest.mark.asyncio
async def test_park_session_stamps_the_real_model_fields(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app_with_env(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        enable_test_endpoints=True,
        monkeypatch=monkeypatch,
    )
    await _seed_session(fake_storage_provider)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/_test/park_session/sess-park-test", json=_park_body(),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "parked_status": "parked"}

    saved = await fake_storage_provider.get_storage(WorkspaceSession).get(
        "sess-park-test",
    )
    assert saved.parked_status == "parked"
    assert saved.parked_event_key == "ask_user:sess-park-test:tc-1"
    assert saved.parked_state["yielded"]["tool_name"] == "ask_user"
    assert saved.parked_at is not None
    # Real park behaviour: no lease is armed by parking alone (the engine
    # drops the lease on a genuine park too) - resuming via the real
    # /ask_user/respond or /tool_approval/respond endpoint is what re-arms
    # it, unmodified production code, not a second injection.
    assert saved.parked_until is None


@pytest.mark.asyncio
async def test_park_session_404s_for_an_unknown_session(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app_with_env(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        enable_test_endpoints=True,
        monkeypatch=monkeypatch,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/_test/park_session/does-not-exist", json=_park_body(),
        )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_park_session_rejects_a_malformed_body(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shape drift fails loudly (422) instead of a raw SQL chain
    silently writing a blob production code can't parse."""
    app = _make_app_with_env(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        enable_test_endpoints=True,
        monkeypatch=monkeypatch,
    )
    await _seed_session(fake_storage_provider)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/_test/park_session/sess-park-test",
            json={"parked_state": {"tool_call_id": "tc-1"}},  # missing parked_event_key
        )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_park_session_returns_404_when_env_unset(
    fake_storage_provider: _FakeStorageProvider,
    fake_provider_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app_with_env(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        enable_test_endpoints=False,
        monkeypatch=monkeypatch,
    )
    await _seed_session(fake_storage_provider)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/_test/park_session/sess-park-test", json=_park_body(),
        )

    assert resp.status_code == 404
