"""Health-check endpoint.

Returns 200 with a stable payload identifying the API. Used by
load-balancers and monitoring to verify the process is responsive.
Does not check downstream dependencies (storage, vector store) — that
is a future ``/v1/ready`` endpoint.

In addition to the always-on ``status`` + ``version`` fields, the
endpoint surfaces a light-touch snapshot of scheduler and worker-pool
state. See spec §14 for the metric set. The full
:meth:`Scheduler.metrics_snapshot` / :meth:`WorkerPool.metrics_snapshot`
payloads are included under the ``.metrics`` sub-keys so dashboards can
scrape ``/v1/health`` without a separate Prometheus exporter.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from matrix.api.version import APP_VERSION


router = APIRouter(tags=["health"])


class SchedulerHealth(BaseModel):
    alive: bool = Field(
        ...,
        description="True when the API process has a live Scheduler instance.",
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Snapshot of in-process scheduler metrics (spec §14). "
            "Empty when the scheduler is absent."
        ),
    )


class WorkerPoolHealth(BaseModel):
    in_flight: int | None = Field(
        default=None,
        description=(
            "Number of sessions currently being executed by this "
            "process's worker pool. Null when the process is API-only."
        ),
    )
    capacity: int | None = Field(
        default=None,
        description=(
            "Configured per-worker concurrency. Null when the process "
            "is API-only."
        ),
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Snapshot of in-process worker-pool metrics (spec §14). "
            "Empty when no pool is attached."
        ),
    )


class HealthStatus(BaseModel):
    status: Literal["ok"] = Field(
        default="ok",
        description="Constant ``ok`` when the process is responsive.",
    )
    version: str = Field(
        ...,
        description="API surface version (semver).",
    )
    scheduler: SchedulerHealth = Field(
        ...,
        description="Liveness + metrics of the in-process scheduler.",
    )
    worker_pool: WorkerPoolHealth = Field(
        ...,
        description="In-flight + capacity + metrics of the worker pool.",
    )


@router.get(
    "/health",
    response_model=HealthStatus,
    summary="Liveness probe",
)
async def health(request: Request) -> HealthStatus:
    scheduler = getattr(request.app.state, "scheduler", None)
    worker_pool = getattr(request.app.state, "worker_pool", None)

    sched_metrics: dict[str, Any] = {}
    if scheduler is not None:
        try:
            sched_metrics = scheduler.metrics_snapshot()
        except Exception:
            # A broken metrics_snapshot must not bring the health
            # endpoint down — fall back to empty.
            sched_metrics = {}

    pool_in_flight: int | None = None
    pool_capacity: int | None = None
    pool_metrics: dict[str, Any] = {}
    if worker_pool is not None:
        try:
            pool_metrics = worker_pool.metrics_snapshot()
        except Exception:
            pool_metrics = {}
        pool_in_flight = pool_metrics.get("matrix_worker_in_flight")
        pool_capacity = pool_metrics.get("matrix_worker_capacity")

    return HealthStatus(
        version=APP_VERSION,
        scheduler=SchedulerHealth(
            alive=scheduler is not None,
            metrics=sched_metrics,
        ),
        worker_pool=WorkerPoolHealth(
            in_flight=pool_in_flight,
            capacity=pool_capacity,
            metrics=pool_metrics,
        ),
    )


__all__ = ["HealthStatus", "SchedulerHealth", "WorkerPoolHealth", "router"]
