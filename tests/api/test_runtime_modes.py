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

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.model.scheduler import (
    InMemorySchedulerConfig,
    RuntimeMode,
    SchedulerProviderConfig,
    SchedulerProviderType,
)
from primer.scheduler.in_memory import InMemoryScheduler

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
        "primer.api.app._build_storage_provider",
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
        "primer.api.app._build_storage_provider",
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
        "primer.api.app._build_storage_provider",
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
        "primer.api.app._build_storage_provider",
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
        "primer.api.app._build_storage_provider",
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


async def test_worker_only_mode_does_not_warm_channels(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """A worker-only process must NOT open inbound channel gateways: it
    relays outbound over the bus instead. Warming belongs to the inbound
    owner."""
    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(runtime_mode=RuntimeMode.WORKER, scheduler=None)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        assert app.state.chat_channel_warm_task is None


async def test_api_plus_worker_mode_warms_channels(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """An inbound-owning process warms the channel adapters.

    It used to run a chat-relay bus forwarder too; that went with the chat
    surface in S6, and the session relay posts through the same adapters
    without a forwarder in between.
    """
    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    cfg = AppConfig(runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        assert app.state.chat_channel_warm_task is not None


async def test_worker_only_mode_does_not_mount_entity_routers(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """In WORKER-only mode only health + workers routers should mount;
    entity routers (workspaces, sessions, etc.) must be absent.

    Asserts on the OpenAPI schema's registered paths rather than scanning
    ``app.routes``: FastAPI 0.138 wraps ``include_router`` results in nested
    ``_IncludedRouter`` containers whose leaf paths are no longer flatly
    enumerable on ``app.routes``, but ``app.openapi()["paths"]`` is the
    stable, prefix-resolved public surface.
    """
    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
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
    paths = set(app.openapi()["paths"].keys())
    assert any("/v1/health" in p for p in paths)
    # /v1/ready rides the same always-on router: a worker-mode process
    # has a database to be unable to reach too.
    assert any("/v1/ready" in p for p in paths)
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
    """Spec §9.1: an in-memory scheduler in a process that only runs a
    worker has a peer API process with its own claim engine: leases and
    resumable parks are not shared, so it is degraded and logs why."""
    import logging
    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
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
    # Capture at the root (the warning is emitted by the primer.api._app_lifespan
    # logger, not primer.api.app) so the degraded-scheduler record is visible.
    with caplog.at_level(logging.WARNING):
        async with app.router.lifespan_context(app):
            reason = app.state.scheduler_degraded_reason
            detail = app.state.scheduler_detail
    assert reason is not None and "runtime_mode=worker" in reason
    assert detail is None
    assert any(
        "in-memory scheduler" in r.message
        and ("multi-process" in r.message or "external-worker" in r.message)
        for r in caplog.records
    )


async def test_in_memory_scheduler_with_api_plus_worker_mode_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C-036 (01a11d04): the default install is one process running the API
    and the worker over the in-memory scheduler. Every claim is made and
    seen by that one process, so nothing is unshared: it is healthy, says
    what it is, and does not log a warning at every boot."""
    import logging
    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
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
    with caplog.at_level(logging.WARNING):
        async with app.router.lifespan_context(app):
            assert app.state.scheduler_degraded_reason is None
            assert app.state.scheduler_detail == "in-memory scheduler (single process assumed)"
    assert not any(
        "in-memory scheduler" in r.message for r in caplog.records
    )


async def test_health_route_reports_the_wired_scheduler_rule(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
) -> None:
    """Both sides of the rule, end to end through GET /v1/health: api+worker
    reads healthy with its detail, a worker-only process reads degraded with
    the reason and no detail."""
    import httpx

    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
        lambda _cfg: mock_storage_provider,
    )
    served: dict[RuntimeMode, dict] = {}
    for mode in (RuntimeMode.API_PLUS_WORKER, RuntimeMode.WORKER):
        cfg = AppConfig(
            runtime_mode=mode,
            scheduler=SchedulerProviderConfig(
                provider=SchedulerProviderType.IN_MEMORY,
                config=InMemorySchedulerConfig(),
            ),
        )
        app = create_app(cfg)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.get("/v1/health")
        assert response.status_code == 200, response.text
        served[mode] = response.json()["scheduler"]

    healthy = served[RuntimeMode.API_PLUS_WORKER]
    assert healthy["alive"] is True
    assert healthy["degraded"] is False
    assert healthy["degraded_reason"] is None
    assert healthy["detail"] == "in-memory scheduler (single process assumed)"

    degraded = served[RuntimeMode.WORKER]
    assert degraded["degraded"] is True
    assert "runtime_mode=worker" in degraded["degraded_reason"]
    assert degraded["detail"] is None


async def test_in_memory_scheduler_with_api_mode_does_not_warn(
    monkeypatch: pytest.MonkeyPatch,
    mock_storage_provider: _FakeStorageProvider,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """API-only mode is fine with in-memory; no warning."""
    import logging
    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
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
    with caplog.at_level(logging.WARNING, logger="primer.api.app"):
        async with app.router.lifespan_context(app):
            pass
    assert not any(
        "in-memory scheduler" in r.message for r in caplog.records
    )
