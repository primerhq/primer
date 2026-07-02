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


async def test_drain_worker_returns_204_and_updates_status(client, app):
    """Drain succeeds for an authenticated caller (``client`` carries the
    signed session cookie)."""
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    resp = await client.post("/v1/workers/w1/drain")
    assert resp.status_code == 204
    workers = await app.state.scheduler.list_workers()
    assert workers[0].status == "draining"


async def test_drain_unknown_worker_is_idempotent(client):
    """Draining a worker that doesn't exist should not error — the
    underlying SQL UPDATE is a no-op when the row is missing."""
    resp = await client.post("/v1/workers/no-such-worker/drain")
    assert resp.status_code == 204


async def test_drain_worker_requires_auth(raw_client, app):
    """POST /workers/{id}/drain is a mutation: an unauthenticated caller
    must be rejected with 401, even though GET /workers stays public for
    liveness/readiness probes."""
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    resp = await raw_client.post("/v1/workers/w1/drain")
    assert resp.status_code == 401
    # The worker must NOT have been drained.
    workers = await app.state.scheduler.list_workers()
    assert workers[0].status != "draining"


async def test_list_workers_stays_public(raw_client):
    """GET /workers remains reachable without auth (probe surface)."""
    resp = await raw_client.get("/v1/workers")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


# ---- DELETE /workers/{id} + POST /workers/purge_dead --------------------


async def test_delete_dead_worker_removes_it(client, app):
    """A dead worker can be removed; the registry row disappears."""
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    app.state.scheduler.mark_worker_dead_for_test("w1")
    resp = await client.delete("/v1/workers/w1")
    assert resp.status_code == 204
    workers = await app.state.scheduler.list_workers()
    assert workers == []


async def test_delete_active_worker_is_rejected(client, app):
    """Deleting a live (active) worker is a 409 — never remove one that
    is still doing work. The row must survive."""
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    resp = await client.delete("/v1/workers/w1")
    assert resp.status_code == 409
    workers = await app.state.scheduler.list_workers()
    assert len(workers) == 1 and workers[0].status == "active"


async def test_delete_draining_worker_is_rejected(client, app):
    """Draining workers are still finishing in-flight leases, so they
    are protected from removal too (409)."""
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    await app.state.scheduler.drain_worker("w1")
    resp = await client.delete("/v1/workers/w1")
    assert resp.status_code == 409
    workers = await app.state.scheduler.list_workers()
    assert len(workers) == 1 and workers[0].status == "draining"


async def test_delete_unknown_worker_is_404(client):
    resp = await client.delete("/v1/workers/no-such-worker")
    assert resp.status_code == 404


async def test_delete_worker_requires_auth(raw_client, app):
    """DELETE is a mutation: an unauthenticated caller is rejected 401
    and the (dead) worker row must survive."""
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    app.state.scheduler.mark_worker_dead_for_test("w1")
    resp = await raw_client.delete("/v1/workers/w1")
    assert resp.status_code == 401
    workers = await app.state.scheduler.list_workers()
    assert len(workers) == 1


async def test_purge_dead_removes_only_dead_and_returns_count(client, app):
    """purge_dead deletes every dead worker, leaves active/draining
    alone, and reports how many it removed."""
    await app.state.scheduler.register_worker(
        worker_id="alive", host="h", pid=1, capacity=4,
    )
    await app.state.scheduler.register_worker(
        worker_id="drainy", host="h", pid=2, capacity=4,
    )
    await app.state.scheduler.drain_worker("drainy")
    for wid in ("d1", "d2", "d3"):
        await app.state.scheduler.register_worker(
            worker_id=wid, host="h", pid=9, capacity=4,
        )
        app.state.scheduler.mark_worker_dead_for_test(wid)

    resp = await client.post("/v1/workers/purge_dead")
    assert resp.status_code == 200
    assert resp.json() == {"removed": 3}

    remaining = {w.id for w in await app.state.scheduler.list_workers()}
    assert remaining == {"alive", "drainy"}


async def test_purge_dead_empty_returns_zero(client):
    resp = await client.post("/v1/workers/purge_dead")
    assert resp.status_code == 200
    assert resp.json() == {"removed": 0}


async def test_purge_dead_requires_auth(raw_client, app):
    await app.state.scheduler.register_worker(
        worker_id="w1", host="h", pid=1, capacity=4,
    )
    app.state.scheduler.mark_worker_dead_for_test("w1")
    resp = await raw_client.post("/v1/workers/purge_dead")
    assert resp.status_code == 401
    workers = await app.state.scheduler.list_workers()
    assert len(workers) == 1
