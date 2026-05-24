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
