"""Tests for matrix.api.app create_app + lifespan wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from matrix.api.app import _build_storage_provider, _make_lifespan, create_app
from matrix.api.config import AppConfig
from matrix.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from matrix.model.scheduler import RuntimeMode
from matrix.storage.sqlite import SqliteStorageProvider


def test_build_storage_provider_defaults_to_sqlite_home_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Redirect HOME so we don't touch the real ~/.matrix.
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = AppConfig()  # db is None
    provider = _build_storage_provider(cfg)
    assert isinstance(provider, SqliteStorageProvider)
    expected_path = tmp_path / ".matrix" / "db" / "data.sqlite"
    assert provider._config.path == expected_path  # noqa: SLF001


def test_build_storage_provider_honours_explicit_sqlite_config(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(
        db=StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=tmp_path / "custom.sqlite"),
        )
    )
    provider = _build_storage_provider(cfg)
    assert isinstance(provider, SqliteStorageProvider)
    assert provider._config.path == tmp_path / "custom.sqlite"  # noqa: SLF001


@pytest.mark.asyncio
async def test_lifespan_zero_config_starts_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = AppConfig(runtime_mode=RuntimeMode.API)
    app = FastAPI(lifespan=_make_lifespan(cfg))
    async with app.router.lifespan_context(app):
        assert isinstance(
            app.state.storage_provider, SqliteStorageProvider,
        )
    # After lifespan exit the provider is closed; nothing should leak.


@pytest.mark.asyncio
async def test_lifespan_with_in_memory_scheduler_sets_claim_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifespan with in-memory scheduler constructs an InMemoryClaimEngine
    and stores it on app.state.claim_engine."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from matrix.model.scheduler import (
        InMemorySchedulerConfig,
        SchedulerProviderConfig,
        SchedulerProviderType,
    )
    cfg = AppConfig(
        runtime_mode=RuntimeMode.API,
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
    )
    app = FastAPI(lifespan=_make_lifespan(cfg))
    async with app.router.lifespan_context(app):
        from matrix.claim.in_memory import InMemoryClaimEngine
        assert isinstance(app.state.claim_engine, InMemoryClaimEngine), (
            f"expected InMemoryClaimEngine, got {type(app.state.claim_engine)}"
        )


def test_docs_endpoints_always_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Swagger + ReDoc + raw OpenAPI surfaces are always mounted
    under /v1, regardless of log_level. The console's 'View OpenAPI'
    button targets /v1/docs and the API itself is exposed at /v1, so
    gating docs behind log_level was security theater and broke the
    affordance."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = AppConfig(runtime_mode=RuntimeMode.API, log_level="info")
    app = create_app(cfg)
    routes = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/v1/docs" in routes
    assert "/v1/redoc" in routes
    assert "/v1/openapi.json" in routes
