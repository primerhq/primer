"""S7 section 6: per-lane counters for the workers page.

A JSON view over the same in-process instruments: the Prometheus text
format is a scrape target, not a UI API.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture(autouse=True)
def _reset_metrics():
    import primer.observability.metrics as m
    m.reset_for_test()
    yield
    m.reset_for_test()


@pytest.mark.asyncio
async def test_empty_when_nothing_has_run(client: httpx.AsyncClient):
    r = await client.get("/v1/workers/stats")
    assert r.status_code == 200, r.text
    assert r.json() == {"items": []}


@pytest.mark.asyncio
async def test_lane_rows_merge_counts_and_durations(client: httpx.AsyncClient):
    import primer.observability.metrics as m

    m.worker_tasks_total.labels("host-0", "session", "ok").inc(3)
    m.worker_task_duration_seconds.labels("host-0", "session", "ok").observe(1.5)
    m.worker_task_duration_seconds.labels("host-0", "session", "ok").observe(0.5)

    r = await client.get("/v1/workers/stats")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["worker"] == "host-0"
    assert row["kind"] == "session"
    assert row["status"] == "ok"
    assert row["tasks"] == 3.0
    assert row["duration_sum_seconds"] == pytest.approx(2.0)
    assert row["duration_count"] == 2.0


@pytest.mark.asyncio
async def test_rows_are_sorted_and_split_per_lane(client: httpx.AsyncClient):
    import primer.observability.metrics as m

    m.worker_tasks_total.labels("host-0", "session", "ok").inc()
    m.worker_tasks_total.labels("host-0", "chat", "error").inc(2)

    r = await client.get("/v1/workers/stats")
    items = r.json()["items"]
    assert [(i["kind"], i["status"]) for i in items] == [
        ("chat", "error"), ("session", "ok"),
    ]
    assert items[0]["tasks"] == 2.0
    assert items[0]["duration_sum_seconds"] == 0.0
