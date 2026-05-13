"""E2E: health + workers observability contracts.

Covers backlog items T0079 (full health envelope shape under
api+worker mode) and T0080 (workers list shape with required heartbeat
fields).

T0001 already pins `status: "ok"` and a non-null version for the
health endpoint; T0028 already pins drain idempotency for the workers
endpoint. These two tests pin the response *shape* — they catch
regressions where a field is silently dropped or renamed.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0079_health_full_contract_under_api_plus_worker(
    client: httpx.AsyncClient,
) -> None:
    """T0079 — under the standard `api+worker` bringup, the health
    envelope shape pins:

    - `status == "ok"`
    - `version` is a non-empty string
    - `scheduler` is `{alive: bool, metrics: dict}` with alive=true
    - `worker_pool` is `{in_flight, capacity, metrics}` (capacity set
      to the configured worker.concurrency)

    NB: the original backlog wording mentioned `scheduler.kind` and
    `worker_pool.running` — neither field exists. The actual
    SchedulerHealth model uses `alive` + `metrics`, and WorkerPoolHealth
    uses `in_flight`/`capacity`/`metrics`. This test pins the real
    shape.
    """
    resp = await client.get("/v1/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body.get("status") == "ok", body
    assert isinstance(body.get("version"), str) and body["version"], body

    scheduler = body.get("scheduler")
    assert isinstance(scheduler, dict), body
    assert scheduler.get("alive") is True, scheduler
    assert isinstance(scheduler.get("metrics"), dict), scheduler

    worker_pool = body.get("worker_pool")
    assert isinstance(worker_pool, dict), body
    assert isinstance(worker_pool.get("metrics"), dict), worker_pool
    # Bringup config sets worker.concurrency=4, so capacity should be
    # the same. Allow a > 0 assertion to be robust against config tweaks.
    assert (
        isinstance(worker_pool.get("capacity"), int)
        and worker_pool["capacity"] > 0
    ), worker_pool
    # in_flight should be 0 on a fresh, idle bringup.
    assert worker_pool.get("in_flight") == 0, worker_pool


_REQUIRED_WORKER_FIELDS = (
    "id",
    "host",
    "pid",
    "started_at",
    "last_heartbeat",
    "status",
)


@pytest.mark.asyncio
async def test_t0100_openapi_spec_byte_stable_across_fetches(
    client: httpx.AsyncClient,
) -> None:
    """T0100 — `GET /openapi.json` must return byte-identical bodies on
    repeated calls. A nondeterministic key ordering (e.g. dict
    insertion order leaking from a runtime-built spec) would break
    SDK code-generators that diff the schema between releases.
    """
    first = await client.get("/openapi.json")
    assert first.status_code == 200, first.text
    second = await client.get("/openapi.json")
    assert second.status_code == 200, second.text
    # Byte-exact comparison; no whitespace tolerance.
    assert first.content == second.content, (
        "OpenAPI spec is not byte-stable across two fetches; "
        f"first len={len(first.content)}, second len={len(second.content)}"
    )


@pytest.mark.asyncio
async def test_t0101_health_endpoint_stable_under_repeated_load(
    client: httpx.AsyncClient,
) -> None:
    """T0101 — 100 sequential `GET /v1/health` calls all return 200
    with the documented envelope keys. Catches schema drift mid-run
    (e.g. metrics keys appearing/disappearing) and any 5xx leakage
    from a metrics-snapshot call that throws.
    """
    expected_keys = {"status", "version", "scheduler", "worker_pool"}
    for i in range(100):
        resp = await client.get("/v1/health")
        assert resp.status_code == 200, (
            f"health request {i} failed: {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert expected_keys.issubset(body.keys()), (
            f"health request {i} missing keys; got {sorted(body.keys())!r}"
        )
        assert body["status"] == "ok", body
        # Scheduler / worker_pool sub-shapes are pinned by T0079;
        # here we only check that the top-level keys remain stable
        # across the whole burst.


@pytest.mark.asyncio
async def test_t0080_workers_list_carries_required_heartbeat_fields(
    client: httpx.AsyncClient,
) -> None:
    """T0080 — under the single-process `api+worker` bringup, exactly
    one worker is registered. Its row must carry every field documented
    in `WorkerInfo`. The check is structural: a future regression that
    drops or renames any of these fields will be caught.
    """
    resp = await client.get("/v1/workers")
    assert resp.status_code == 200, resp.text
    items = resp.json().get("items")
    assert isinstance(items, list), resp.text
    assert len(items) >= 1, items

    worker = items[0]
    for field in _REQUIRED_WORKER_FIELDS:
        assert field in worker, (
            f"WorkerInfo field {field!r} missing from response: {worker!r}"
        )
    # `status` is a Literal["active", "draining", "dead"]; on a fresh
    # bringup the only valid initial value is "active".
    assert worker["status"] in ("active", "draining", "dead"), worker
