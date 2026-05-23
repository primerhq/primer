"""Tests for runtime_mode lifespan wiring + mount-mode awareness.

Covers Task 23 of the background-execution-scheduler plan: the API
lifespan must validate that ``runtime_mode in (worker, api+worker)``
has a scheduler configured, must build + attach the scheduler and
worker pool to ``app.state``, must drain on shutdown, and
``_mount_routers`` must skip entity routers when running in pure
``WORKER`` mode.
"""

from __future__ import annotations

import asyncio

import pytest

from matrix.api.app import create_app
from matrix.api.config import AppConfig
from matrix.model.except_ import ConfigError
from matrix.model.scheduler import (
    InMemorySchedulerConfig,
    RuntimeMode,
    SchedulerProviderConfig,
    SchedulerProviderType,
)

from tests.api.conftest import _FakeStorageProvider


@pytest.fixture
def mock_storage_provider() -> _FakeStorageProvider:
    """Reuses the in-memory ``_FakeStorageProvider`` from the suite-wide
    conftest. Exposed here under the name the spec calls for."""
    return _FakeStorageProvider()


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATRIX_DB_HOST", "localhost")
    monkeypatch.setenv("MATRIX_DB_DATABASE", "matrix")
    monkeypatch.setenv("MATRIX_DB_USER", "u")
    monkeypatch.setenv("MATRIX_DB_PASSWORD", "p")


def test_worker_mode_without_scheduler_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """Lifespan must reject WORKER mode without a scheduler configured."""
    _base_env(monkeypatch)
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(runtime_mode=RuntimeMode.WORKER, scheduler=None)
    app = create_app(cfg)

    async def _run() -> None:
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(ConfigError):
        asyncio.run(_run())


def test_api_plus_worker_mode_without_scheduler_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """API_PLUS_WORKER mode also requires a scheduler."""
    _base_env(monkeypatch)
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None)
    app = create_app(cfg)

    async def _run() -> None:
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(ConfigError):
        asyncio.run(_run())


async def test_api_only_mode_does_not_start_worker_pool(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """API mode with a scheduler still does not start a worker pool."""
    _base_env(monkeypatch)
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(
        runtime_mode=RuntimeMode.API,
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
    )
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        assert app.state.worker_pool is None
        assert app.state.scheduler is not None


async def test_api_only_mode_without_scheduler_is_ok(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """Pure API mode does not require a scheduler."""
    _base_env(monkeypatch)
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(runtime_mode=RuntimeMode.API, scheduler=None)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        assert app.state.worker_pool is None
        assert app.state.scheduler is None


async def test_api_plus_worker_mode_starts_worker_pool(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """API_PLUS_WORKER mode wires both the scheduler and the worker pool,
    and the pool registers itself with the scheduler at startup."""
    _base_env(monkeypatch)
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(
        runtime_mode=RuntimeMode.API_PLUS_WORKER,
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
    )
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        assert app.state.worker_pool is not None
        assert app.state.scheduler is not None
        # The pool must have registered itself with the scheduler.
        workers = await app.state.scheduler.list_workers()
        assert len(workers) == 1


async def test_worker_only_mode_does_not_mount_entity_routers(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """In WORKER-only mode only health + workers routers should mount;
    entity routers (workspaces, sessions, etc.) must be absent."""
    _base_env(monkeypatch)
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(
        runtime_mode=RuntimeMode.WORKER,
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
    )
    app = create_app(cfg)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert any("/v1/health" in p for p in paths)
    assert any("/v1/workers" in p for p in paths)
    assert not any("/v1/workspaces" in p for p in paths)
    assert not any("/v1/sessions" in p for p in paths)
    assert not any("/v1/providers" in p for p in paths)
    assert not any("/v1/collections" in p for p in paths)


async def test_in_memory_scheduler_with_worker_mode_emits_warning(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec §9.1: in-memory scheduler + non-API mode should log a
    warning about multi-worker safety."""
    import logging
    _base_env(monkeypatch)
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(
        runtime_mode=RuntimeMode.API_PLUS_WORKER,
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
    )
    app = create_app(cfg)
    with caplog.at_level(logging.WARNING, logger="matrix.api.app"):
        async with app.router.lifespan_context(app):
            pass
    assert any(
        "in-memory scheduler" in r.message and "multi-worker" in r.message
        for r in caplog.records
    )


async def test_in_memory_scheduler_with_api_mode_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """API-only mode is fine with in-memory; no warning."""
    import logging
    _base_env(monkeypatch)
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(
        runtime_mode=RuntimeMode.API,
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
    )
    app = create_app(cfg)
    with caplog.at_level(logging.WARNING, logger="matrix.api.app"):
        async with app.router.lifespan_context(app):
            pass
    assert not any(
        "in-memory scheduler" in r.message for r in caplog.records
    )
