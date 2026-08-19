"""Worker observability + lifecycle endpoints.

Operators read ``GET /v1/workers`` to see which scheduler-registered
workers are alive (and their last heartbeat). ``POST /v1/workers/{id}/drain``
marks one as draining so other workers take over its sessions at the
next turn boundary. Once a worker has been reaped to ``dead`` (it stopped
heart-beating) its row lingers in the registry forever, so
``DELETE /v1/workers/{id}`` removes a single dead worker and
``POST /v1/workers/purge_dead`` clears every dead worker in one call.

The actual lifecycle (registration, heartbeats, turn execution) is
owned by ``WorkerPool``; this router just exposes the read/drain/remove
surface backed by ``Scheduler.list_workers`` / ``drain_worker`` /
``deregister_worker``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from primer.api.deps import get_scheduler, require_auth
from primer.api.errors import common_responses
from primer.model.except_ import ConflictError, NotFoundError


router = APIRouter(tags=["workers"])


@router.get(
    "/workers",
    summary="List registered workers",
    responses=common_responses(500),
)
async def list_workers(scheduler=Depends(get_scheduler)) -> dict:
    workers = await scheduler.list_workers()
    return {"items": [w.model_dump(mode="json") for w in workers]}


@router.get(
    "/workers/stats",
    summary="Per-lane task counters read from the in-process instruments",
    responses=common_responses(500),
)
async def worker_lane_stats() -> dict:
    """Aggregate ``worker_tasks_total`` + ``worker_task_duration_seconds``.

    The workers page needs numbers, not Prometheus exposition text: the
    text format is a scrape target, not a UI API (12-s7-design.md section
    6). Values are this process's own instruments, so a multi-process
    deployment reports per-process figures, exactly like /v1/health's
    worker_pool block.
    """
    import primer.observability.metrics as _metrics

    rows: dict[tuple[str, str, str], dict] = {}

    def _row(labels: dict) -> dict:
        key = (labels["worker"], labels["kind"], labels["status"])
        if key not in rows:
            rows[key] = {
                "worker": key[0],
                "kind": key[1],
                "status": key[2],
                "tasks": 0.0,
                "duration_sum_seconds": 0.0,
                "duration_count": 0.0,
            }
        return rows[key]

    for metric in _metrics.worker_tasks_total.collect():
        for sample in metric.samples:
            if sample.name == "worker_tasks_total":
                _row(sample.labels)["tasks"] = sample.value
    for metric in _metrics.worker_task_duration_seconds.collect():
        for sample in metric.samples:
            if sample.name == "worker_task_duration_seconds_sum":
                _row(sample.labels)["duration_sum_seconds"] = sample.value
            elif sample.name == "worker_task_duration_seconds_count":
                _row(sample.labels)["duration_count"] = sample.value

    return {"items": [rows[key] for key in sorted(rows)]}


@router.post(
    "/workers/{worker_id}/drain",
    status_code=204,
    summary="Mark a worker as draining (other workers take over its sessions)",
    responses=common_responses(401, 500),
    # Mutating endpoint: requires auth even though the router as a whole
    # is mounted public so liveness/readiness probes can read GET /workers
    # pre-login. require_auth no-ops under auth-disabled (the middleware
    # injects a synthetic system user), so dogfood is unaffected.
    dependencies=[Depends(require_auth)],
)
async def drain_worker(
    worker_id: str = Path(...),
    scheduler=Depends(get_scheduler),
) -> None:
    await scheduler.drain_worker(worker_id)


@router.post(
    "/workers/purge_dead",
    summary="Remove every dead worker from the registry",
    responses=common_responses(401, 500),
    dependencies=[Depends(require_auth)],
)
async def purge_dead_workers(scheduler=Depends(get_scheduler)) -> dict:
    """Bulk-remove all workers currently in the ``dead`` state.

    Dead workers are rows the scheduler reaped after they stopped
    heart-beating; they never come back on their own, so this frees the
    registry of accumulated tombstones. Returns the number removed.
    Active/draining workers are never touched.
    """
    removed = await scheduler.purge_dead_workers()
    return {"removed": removed}


@router.delete(
    "/workers/{worker_id}",
    status_code=204,
    summary="Remove a single dead worker from the registry",
    responses=common_responses(401, 404, 409, 500),
    # Mutating endpoint: requires auth (see drain_worker note).
    dependencies=[Depends(require_auth)],
)
async def delete_worker(
    worker_id: str = Path(...),
    scheduler=Depends(get_scheduler),
) -> None:
    """Deregister one worker — but only if it is ``dead``.

    Refuses (409) to remove an ``active`` or ``draining`` worker: those
    are still (or soon to be) doing work and their rows are managed by
    the heartbeat/drain lifecycle. Unknown ids are 404.
    """
    workers = await scheduler.list_workers()
    match = next((w for w in workers if w.id == worker_id), None)
    if match is None:
        raise NotFoundError(f"Worker {worker_id!r} does not exist")
    if match.status != "dead":
        raise ConflictError(
            f"Worker {worker_id!r} is {match.status!r}, not dead; only dead "
            "workers can be removed. Drain it and let it be reaped first."
        )
    await scheduler.deregister_worker(worker_id)


__all__ = ["router", "worker_lane_stats"]
