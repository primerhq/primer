"""Tests for the /v1/workers REST surface."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport


@pytest.fixture
async def workers_client(app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as c:
        yield c


async def test_list_workers_empty(workers_client):
    resp = await workers_client.get("/v1/workers")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": []}


async def test_list_workers_after_register(workers_client, app):
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    resp = await workers_client.get("/v1/workers")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == "w1"
    assert items[0]["capacity"] == 4


async def test_drain_worker_returns_204_and_updates_status(workers_client, app):
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    resp = await workers_client.post("/v1/workers/w1/drain")
    assert resp.status_code == 204
    workers = await app.state.scheduler.list_workers()
    assert workers[0].status == "draining"


async def test_drain_unknown_worker_is_idempotent(workers_client):
    """Draining a worker that doesn't exist should not error — the
    underlying SQL UPDATE is a no-op when the row is missing."""
    resp = await workers_client.post("/v1/workers/no-such-worker/drain")
    assert resp.status_code == 204
