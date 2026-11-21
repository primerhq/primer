"""Repairable projection of durable memory requests onto claim leases."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

from primer.int.claim import ClaimKind
from primer.model.memory import MemoryJob
from primer.model.storage import CursorPage
from primer.storage.q import Q

logger = logging.getLogger(__name__)


def new_occurrence() -> str:
    return uuid4().hex


@asynccontextmanager
async def locked_occurrence(sp, job: MemoryJob, *, engine=None, lease=None):
    """Lock job then lease. Keep the in-process acquisition pinned through commit."""
    async with AsyncExitStack() as receipts:
        async with sp.locking_transaction(models=(MemoryJob,)) as conn:
            current = await sp.get_storage(MemoryJob).get_for_update(job.id, conn=conn)
            if current is not None and current.occurrence_guard() != job.occurrence_guard():
                current = None
            if current is not None and lease is not None:
                if lease.memory_claim_id is not None and engine is not None:
                    owned = await receipts.enter_async_context(engine.hold_memory_claim(lease, conn=conn))
                else:
                    owned = current.occurrence_id is None and lease.memory_claim_id is None
                if not owned:
                    current = None
            yield current, conn


async def patch_job_progress(deps, job: MemoryJob, patch: dict) -> None:
    async with locked_occurrence(deps.sp, job, engine=deps.engine, lease=deps.lease) as (current, conn):
        if current is not None:
            await deps.sp.get_storage(MemoryJob).patch_if(job.id, patch, where=job.occurrence_guard(), conn=conn)


async def arm_current(sp, engine, job_id: str) -> None:
    """After intent commit, serialize projections against the *current* row.

    An old enqueue or repair may arrive last. It can only publish the newest
    committed deadline. PG uses this second transaction's own connection.
    In-process publication is synchronous under the read lock and replaceable
    from storage after a restart. Neither path holds a lock across model work.
    """
    if engine is None:
        return
    async with sp.locking_transaction(models=(MemoryJob,)) as conn:
        job = await sp.get_storage(MemoryJob).get_for_update(job_id, conn=conn)
        if job is not None and job.occurrence_id is not None and job.requested_due_at is not None:
            await engine.arm_memory_job(job, conn=conn)


async def reconcile_page(sp, engine, cursor: str | None = None, *, length: int = 100) -> str | None:
    """One bounded keyset page. Legacy/terminal rows have no recoverable deadline."""
    pending = Q(MemoryJob).where_not_null("requested_due_at").build()
    page = await sp.get_storage(MemoryJob).find(pending, CursorPage(cursor=cursor, length=length))
    for job in page.items:
        try:
            await arm_current(sp, engine, job.id)
        except Exception:
            logger.exception("memory lease repair failed for %s", job.id)
    if page.next_cursor is None:
        # Death after entity deletion but before lease release leaves a join
        # miss. Pruning checks the observed occurrence again at deletion.
        await engine.prune_dead_leases(ClaimKind.MEMORY)
    return page.next_cursor
