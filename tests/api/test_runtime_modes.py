"""Tests for runtime_mode lifespan wiring + mount-mode awareness.

Covers Task 23 of the background-execution-scheduler plan: the API
lifespan must build + attach the scheduler and worker pool to
``app.state``, must drain on shutdown, and ``_mount_routers`` must skip
entity routers when running in pure ``WORKER`` mode.

Task 9 update: ``scheduler=None`` no longer raises ConfigError; instead
the lifespan defaults to an in-memory scheduler when runtime_mode
requires a worker.
"""

from __future__ import annotations

import asyncio

import pytest

from matrix.api.app import create_app
from matrix.api.config import AppConfig
from matrix.model.scheduler import (
    InMemorySchedulerConfig,
    RuntimeMode,
    SchedulerProviderConfig,
    SchedulerProviderType,
)
from matrix.scheduler.in_memory import InMemoryScheduler

from tests.api.conftest import _FakeStorageProvider


@pytest.fixture
def mock_storage_provider() -> _FakeStorageProvider:
    """Reuses the in-memory ``_FakeStorageProvider`` from the suite-wide
    conftest. Exposed here under the name the spec calls for."""
    return _FakeStorageProvider()


async def test_api_only_mode_does_not_start_worker_pool(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """API mode with a scheduler still does not start a worker pool."""
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
    """Pure API mode does not require a scheduler; scheduler stays None."""
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(runtime_mode=RuntimeMode.API, scheduler=None)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        assert app.state.worker_pool is None
        assert app.state.scheduler is None


async def test_worker_mode_without_scheduler_defaults_to_in_memory(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """WORKER mode with scheduler=None now boots with an in-memory scheduler."""
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(runtime_mode=RuntimeMode.WORKER, scheduler=None)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.scheduler, InMemoryScheduler)
        assert app.state.worker_pool is not None


async def test_api_plus_worker_mode_without_scheduler_defaults_to_in_memory(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """API_PLUS_WORKER mode with scheduler=None now boots with an in-memory scheduler."""
    monkeypatch.setattr(
        "matrix.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.scheduler, InMemoryScheduler)
        assert app.state.worker_pool is not None


async def test_api_plus_worker_mode_starts_worker_pool(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """API_PLUS_WORKER mode wires both the scheduler and the worker pool,
    and the pool registers itself with the scheduler at startup."""
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
