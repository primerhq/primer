"""Smoke test for GET /v1/health."""

from __future__ import annotations

import pytest

from matrix.api.version import APP_VERSION


@pytest.mark.asyncio
async def test_health_returns_ok(client) -> None:
    response = await client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == APP_VERSION


@pytest.mark.asyncio
async def test_health_surfaces_scheduler_alive(client, app) -> None:
    """/v1/health includes scheduler.alive + scheduler.metrics (spec §14)."""
    response = await client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert "scheduler" in body
    # The test app wires an InMemoryScheduler in create_test_app, so
    # alive should be True and the metrics dict should carry the
    # spec §14 keys.
    assert body["scheduler"]["alive"] is True
    metrics = body["scheduler"]["metrics"]
    assert "matrix_sessions_active" in metrics
    assert "matrix_sessions_runnable_queue_depth" in metrics
    assert "matrix_scheduler_notify_received_total" in metrics


@pytest.mark.asyncio
async def test_health_surfaces_worker_pool_in_flight_capacity(client) -> None:
    """/v1/health includes worker_pool.in_flight + worker_pool.capacity.

    The test app does not run a real WorkerPool (worker_pool=None), so
    in_flight + capacity should both be null but the keys must exist.
    """
    response = await client.get("/v1/health")
    body = response.json()
    assert "worker_pool" in body
    assert body["worker_pool"]["in_flight"] is None
    assert body["worker_pool"]["capacity"] is None


@pytest.mark.asyncio
async def test_health_surfaces_worker_pool_metrics_when_attached(
    app, client,
) -> None:
    """When app.state.worker_pool is set, /v1/health surfaces its
    in_flight + capacity from the metrics snapshot."""
    from matrix.model.scheduler import WorkerConfig
    from matrix.worker.pool import WorkerPool

    pool = WorkerPool(
        config=WorkerConfig(concurrency=5),
        scheduler=app.state.scheduler,
        storage=None,                  # type: ignore[arg-type]
        workspace_registry=None,       # type: ignore[arg-type]
        provider_registry=None,        # type: ignore[arg-type]
    )
    app.state.worker_pool = pool
    try:
        response = await client.get("/v1/health")
        body = response.json()
        assert body["worker_pool"]["in_flight"] == 0
        assert body["worker_pool"]["capacity"] == 5
        metrics = body["worker_pool"]["metrics"]
        assert metrics["matrix_worker_capacity"] == 5
        assert "matrix_session_turns_total" in metrics
        assert "matrix_session_turn_duration_seconds" in metrics
    finally:
        app.state.worker_pool = None
