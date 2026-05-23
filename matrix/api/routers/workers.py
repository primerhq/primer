"""Worker observability + drain endpoint.

Operators read ``GET /v1/workers`` to see which scheduler-registered
workers are alive (and their last heartbeat). ``POST /v1/workers/{id}/drain``
marks one as draining so other workers take over its sessions at the
next turn boundary.

The actual lifecycle (registration, heartbeats, turn execution) is
owned by ``WorkerPool``; this router just exposes the read/drain
surface backed by ``Scheduler.list_workers`` / ``drain_worker``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from matrix.api.deps import get_scheduler
from matrix.api.errors import common_responses


router = APIRouter(tags=["workers"])


@router.get(
    "/workers",
    summary="List registered workers",
    responses=common_responses(500),
)
async def list_workers(scheduler=Depends(get_scheduler)) -> dict:
    workers = await scheduler.list_workers()
    return {"items": [w.model_dump(mode="json") for w in workers]}


@router.post(
    "/workers/{worker_id}/drain",
    status_code=204,
    summary="Mark a worker as draining (other workers take over its sessions)",
    responses=common_responses(500),
)
async def drain_worker(
    worker_id: str = Path(...),
    scheduler=Depends(get_scheduler),
) -> None:
    await scheduler.drain_worker(worker_id)


__all__ = ["router"]
