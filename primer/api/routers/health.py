"""Health-check endpoint.

Returns 200 with a stable payload identifying the API. Used by
load-balancers and monitoring to verify the process is responsive.
Does not check downstream dependencies (storage, vector store): the
database is checked by ``/v1/ready`` below, which is deliberately not
wired to any probe (see its docstring before changing that).

In addition to the always-on ``status`` + ``version`` fields, the
endpoint surfaces a light-touch snapshot of scheduler and worker-pool
state. See spec §14 for the metric set. The full
:meth:`Scheduler.metrics_snapshot` / :meth:`WorkerPool.metrics_snapshot`
payloads are included under the ``.metrics`` sub-keys so dashboards can
scrape ``/v1/health`` without a separate Prometheus exporter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from primer.api.version import APP_VERSION


logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Bounds the database round-trip below the pool's own 30s acquire timeout:
# a monitor polling /v1/ready wants an answer, not a 30s hang that looks
# the same as the outage it is trying to report.
_READY_DB_TIMEOUT_S = 3.0


class SchedulerHealth(BaseModel):
    alive: bool = Field(
        ...,
        description="True when the API process has a live Scheduler instance.",
    )
    degraded: bool = Field(
        default=False,
        description=(
            "True when the wired scheduler/runtime-mode combination is "
            "unsafe for the deployment topology (e.g. an in-memory "
            "scheduler in a multi-process or external-worker mode, where "
            "leases and resumable parks are not shared across processes)."
        ),
    )
    degraded_reason: str | None = Field(
        default=None,
        description=(
            "Human-readable explanation of the degraded condition, or null "
            "when the scheduler configuration is healthy."
        ),
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
            "This process's own worker-pool concurrency, or (when this "
            "process has no local pool attached) the durable scheduler "
            "registry's summed capacity across live workers. Null only "
            "when neither source is available (no local pool and no "
            "scheduler)."
        ),
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Snapshot of in-process worker-pool metrics (spec §14). "
            "Empty when no pool is attached."
        ),
    )


class LocalWorkspacesHealth(BaseModel):
    distributed: bool = Field(
        ...,
        description=(
            "True when this deployment's topology means more than one process could touch a local workspace: "
            "runtime_mode is not 'api+worker', or the scheduler is postgres."
        ),
    )
    enforcing: bool = Field(
        ...,
        description="The local_workspaces.refuse_when_distributed switch (off by default).",
    )
    single_process: bool = Field(
        ...,
        description=(
            "The local_workspaces.single_process operator assertion that exactly one process runs everything."
        ),
    )
    refusing_local: bool = Field(
        ...,
        description=(
            "True when this process refuses to hand out a workspace on a local provider "
            "(enforcing, distributed and not single_process)."
        ),
    )
    signals: list[str] = Field(
        default_factory=list,
        description="Which topology signals made the deployment distributed; empty when it is not.",
    )
    local_workspaces: int | None = Field(
        default=None,
        description=(
            "Workspaces (any phase) on a local provider, as of the workspace probe's last tick; null before the "
            "first tick. Reported with the switch OFF too, so an operator can see what turning it on would strand."
        ),
    )
    unusable: int | None = Field(
        default=None,
        description=(
            "Workspaces this process refuses right now: local_workspaces while refusing_local, else 0; null when "
            "refusing but not counted yet (unknown, not zero)."
        ),
    )


class WorkspacesHealth(BaseModel):
    local: LocalWorkspacesHealth = Field(
        ...,
        description="Whether, and how much of, the deployment's local-provider workspaces this process refuses.",
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
    workspaces: WorkspacesHealth | None = Field(
        default=None,
        description=(
            "The local-provider workspace rule (ticket 01a1072f). Null when this process has no workspace "
            "registry to ask."
        ),
    )


def _workspaces_health(request: Request) -> WorkspacesHealth | None:
    """The local-workspace rule and its census, from in-process state only (this route must stay 200 without the DB)."""
    registry = getattr(request.app.state, "workspace_registry", None)
    policy = getattr(registry, "local_policy", None)
    if policy is None:
        return None
    census = getattr(getattr(request.app.state, "workspace_probe", None), "local_workspace_count", None)
    described = policy.describe()
    return WorkspacesHealth(local=LocalWorkspacesHealth(
        distributed=described["distributed"],
        enforcing=described["enforcing"],
        single_process=described["single_process"],
        refusing_local=described["refusing_local"],
        signals=described["signals"],
        local_workspaces=census,
        unusable=(census if described["refusing_local"] else 0),
    ))


@router.get(
    "/health",
    response_model=HealthStatus,
    summary="Process health",
)
async def health(request: Request) -> HealthStatus:
    """Answer 200 whenever this process is serving HTTP.

    That is all it proves: it reads in-process state only, so it stays 200
    with the database gone (``/v1/ready`` is the database check). Consumed
    by the container ``HEALTHCHECK`` and by the deployment's Kubernetes
    readiness probe.
    """
    scheduler = getattr(request.app.state, "scheduler", None)
    worker_pool = getattr(request.app.state, "worker_pool", None)
    degraded_reason = getattr(
        request.app.state, "scheduler_degraded_reason", None
    )

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
        pool_in_flight = pool_metrics.get("primer_worker_in_flight")
        pool_capacity = pool_metrics.get("primer_worker_capacity")

    if pool_capacity is None and scheduler is not None:
        # This process has no local worker pool attached (API-only, per
        # the field docstring) — worker pods own the pool in this
        # topology, so the in-process metrics snapshot above can never
        # answer capacity here. Fall back to the durable scheduler
        # registry, summing live (non-dead) workers' capacity, the same
        # aggregate the Workers page computes client-side. in_flight has
        # no durable equivalent (per-worker live load is process-local
        # and never persisted) so it is left null rather than guessed.
        try:
            workers = await scheduler.list_workers()
            pool_capacity = sum(w.capacity for w in workers if w.status != "dead")
        except Exception:
            pool_capacity = None

    return HealthStatus(
        workspaces=_workspaces_health(request),
        version=APP_VERSION,
        scheduler=SchedulerHealth(
            alive=scheduler is not None,
            degraded=degraded_reason is not None,
            degraded_reason=degraded_reason,
            metrics=sched_metrics,
        ),
        worker_pool=WorkerPoolHealth(
            in_flight=pool_in_flight,
            capacity=pool_capacity,
            metrics=pool_metrics,
        ),
    )


class DatabaseReadiness(BaseModel):
    ok: bool = Field(
        ...,
        description="True when a real round-trip to the database succeeded.",
    )
    latency_ms: float = Field(
        ...,
        description=(
            "Wall time of the round-trip, in milliseconds. On failure this "
            "is the time until the failure was observed (the timeout, for "
            "a hang)."
        ),
    )
    error: Literal["timeout", "error"] | None = Field(
        default=None,
        description=(
            "Null when ok. ``timeout``: no answer within the bound. "
            "``error``: the round-trip raised. A stable code only - the "
            "exception text can carry hostnames, ports and usernames and "
            "this route is unauthenticated, so the detail goes to the "
            "server log instead."
        ),
    )


class ReadyChecks(BaseModel):
    database: DatabaseReadiness


class ReadyStatus(BaseModel):
    status: Literal["ready", "not_ready"] = Field(
        ...,
        description="``ready`` only when every check passed.",
    )
    version: str = Field(..., description="API surface version (semver).")
    checks: ReadyChecks


@router.get(
    "/ready",
    response_model=ReadyStatus,
    responses={
        503: {
            "model": ReadyStatus,
            "description": "A check failed; the body says which.",
        },
    },
    summary="Database reachability",
)
async def ready(request: Request) -> JSONResponse:
    """Report whether the database answers a real round-trip.

    Unlike ``/v1/health`` (in-process state only, so it stays 200 with the
    database gone), this issues one ``SELECT 1`` through the storage
    provider. 200 when it answers, 503 with the same body shape when it
    does not, so a monitor reading only the status code gets the truth.

    DELIBERATELY NOT WIRED TO ANY KUBERNETES PROBE. Reread this before
    changing that:

    * Readiness: the deployment runs a single API replica, and its
      HTTPRoute sends all traffic to that one Service. Failing readiness
      on a database blip empties the Endpoints list and turns a
      recoverable error (the app keeps serving its own 500s and heals the
      moment the database returns) into a hard outage with only a bare
      gateway 503 to show for it. Readiness exists to route AROUND a bad
      instance; with one replica there is nowhere to route.
    * Startup: with the database unreachable at boot the lifespan raises
      and the process exits (``storage_provider.initialize()`` is
      unconditional), so kubelet's restart policy already produces the
      crash loop with no probe involved. There is no "running but
      database-less" state for a startup probe to observe.
    * The slow-but-not-down boot window is already closed by the HTTP
      readiness probe on ``/v1/health``: it cannot answer until the
      lifespan, including pool init, has completed.

    What would make wiring readiness to this safe: more than one API
    replica, so a pod that loses the database has a healthy sibling to
    shed traffic to.
    """
    storage = getattr(request.app.state, "storage_provider", None)
    started = time.perf_counter()
    error: Literal["timeout", "error"] | None = None
    if storage is None:
        error = "error"
        logger.warning("/v1/ready: no storage provider on app.state")
    else:
        try:
            await asyncio.wait_for(storage.ping(), timeout=_READY_DB_TIMEOUT_S)
        except TimeoutError:
            error = "timeout"
            logger.warning(
                "/v1/ready: database round-trip exceeded %.1fs",
                _READY_DB_TIMEOUT_S,
            )
        except Exception:
            error = "error"
            logger.warning("/v1/ready: database round-trip failed", exc_info=True)
    latency_ms = round((time.perf_counter() - started) * 1000, 1)

    body = ReadyStatus(
        status="ready" if error is None else "not_ready",
        version=APP_VERSION,
        checks=ReadyChecks(
            database=DatabaseReadiness(
                ok=error is None, latency_ms=latency_ms, error=error,
            ),
        ),
    )
    return JSONResponse(
        status_code=200 if error is None else 503,
        content=body.model_dump(mode="json"),
    )


__all__ = [
    "DatabaseReadiness",
    "HealthStatus",
    "ReadyChecks",
    "ReadyStatus",
    "SchedulerHealth",
    "WorkerPoolHealth",
    "router",
]
