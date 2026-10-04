

# ---------------------------------------------------------------------------
# Bulk dead-worker purge
# ---------------------------------------------------------------------------
# A registry accumulates one tombstone per worker per restart, so this runs
# against hundreds of rows in practice. It replaced a loop over
# deregister_worker, which cost one connection acquire + DELETE each.


class TestPurgeDeadWorkers:
    async def test_removes_only_dead_workers(self, scheduler) -> None:
        await scheduler.register_worker(worker_id="wrk-live", host="h", pid=1, capacity=3)
        await scheduler.register_worker(worker_id="wrk-dead", host="h", pid=2, capacity=3)
        await _force_dead(scheduler, "wrk-dead")

        removed = await scheduler.purge_dead_workers()

        assert removed == 1
        remaining = {w.id for w in await scheduler.list_workers()}
        assert remaining == {"wrk-live"}

    async def test_returns_zero_on_a_clean_registry(self, scheduler) -> None:
        await scheduler.register_worker(worker_id="wrk-live", host="h", pid=1, capacity=3)
        assert await scheduler.purge_dead_workers() == 0

    async def test_is_idempotent(self, scheduler) -> None:
        await scheduler.register_worker(worker_id="wrk-dead", host="h", pid=1, capacity=3)
        await _force_dead(scheduler, "wrk-dead")
        assert await scheduler.purge_dead_workers() == 1
        assert await scheduler.purge_dead_workers() == 0


async def _force_dead(scheduler, worker_id: str) -> None:
    """Mark a worker dead through whichever surface the backend exposes."""
    marker = getattr(scheduler, "_mark_worker_dead", None)
    if marker is not None:
        await marker(worker_id)
        return
    entry = getattr(scheduler, "_workers", {}).get(worker_id)
    if entry is not None:
        entry.info.status = "dead"
        return
    pool = getattr(getattr(scheduler, "_storage", None), "pool", None)
    if pool is not None:
        # PostgresScheduler has exactly one way to mark a worker dead: the
        # boot-time recovery sweep in initialize() flips any worker whose
        # heartbeat is older than 5 minutes. Backdate this worker's
        # heartbeat, then re-run initialize() (idempotent) so the real
        # transition produces the tombstone rather than faking the status.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE workers SET last_heartbeat = now() - interval '10 minutes' "
                "WHERE id = $1",
                worker_id,
            )
        await scheduler.initialize()
        statuses = {w.id: w.status for w in await scheduler.list_workers()}
        assert statuses.get(worker_id) == "dead", (
            f"boot recovery did not mark {worker_id!r} dead: {statuses}"
        )
        return
    raise AssertionError("no way to mark a worker dead on this backend")
