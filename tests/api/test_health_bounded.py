"""GS-3: /v1/health answers with a null capacity when the scheduler's database read hangs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from primer.api.routers.health import health


class _Scheduler:
    def __init__(self, hang: bool):
        self.hang = hang

    def metrics_snapshot(self) -> dict:
        return {}

    async def list_workers(self):
        if self.hang:
            await asyncio.sleep(3600)
        return [SimpleNamespace(status="alive", capacity=4, in_flight=1)]


def _request(scheduler):
    state = SimpleNamespace(scheduler=scheduler, worker_pool=None, scheduler_degraded_reason=None,
                            scheduler_detail=None, workspace_registry=None)
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_health_answers_when_the_database_read_hangs():
    body = await asyncio.wait_for(health(_request(_Scheduler(hang=True))), 3.0)
    assert body.worker_pool.capacity is None and body.worker_pool.in_flight is None


async def test_health_still_sums_live_workers():
    body = await health(_request(_Scheduler(hang=False)))
    assert (body.worker_pool.capacity, body.worker_pool.in_flight) == (4, 1)
