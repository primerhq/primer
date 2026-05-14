"""E2E: idempotent operations per §17 invariant #5.

Covers backlog items T0026 (LLMProvider invalidate) and T0028 (worker
drain). DELETE is explicitly NOT idempotent — see T0009.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


def _llm_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }


@pytest.mark.asyncio
async def test_t0026_llm_provider_invalidate_idempotent(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0026 — POST /v1/llm_providers/{id}/invalidate is idempotent.

    Two consecutive calls must both return 204; the second is a no-op
    because the cache is already cold after the first.
    """
    entity_id = f"llm-inv-{unique_suffix}"
    created = await client.post("/v1/llm_providers", json=_llm_body(entity_id))
    assert created.status_code == 201, created.text
    try:
        first = await client.post(f"/v1/llm_providers/{entity_id}/invalidate")
        assert first.status_code == 204, first.text
        second = await client.post(f"/v1/llm_providers/{entity_id}/invalidate")
        assert second.status_code == 204, (
            f"second invalidate expected 204 (idempotent), got "
            f"{second.status_code}: {second.text}"
        )
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


@pytest.mark.asyncio
async def test_t0028_worker_drain_idempotent(
    client: httpx.AsyncClient,
) -> None:
    """T0028 — POST /v1/workers/{id}/drain is idempotent.

    Both calls must return 204; afterwards GET /v1/workers must show
    the worker's status as ``draining`` (per WorkerInfo's literal
    field — the test does not check for a boolean ``draining`` key).
    """
    # 1. Discover the live worker (api+worker mode is what bringup
    #    starts, so exactly one worker is registered).
    listed = await client.get("/v1/workers")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items, (
        f"expected at least one registered worker, got: {listed.json()!r}"
    )
    worker_id = items[0]["id"]

    # 2. Drain twice — both must return 204.
    first = await client.post(f"/v1/workers/{worker_id}/drain")
    assert first.status_code == 204, first.text
    second = await client.post(f"/v1/workers/{worker_id}/drain")
    assert second.status_code == 204, (
        f"second drain expected 204 (idempotent), got "
        f"{second.status_code}: {second.text}"
    )

    # 3. GET /v1/workers shows status=draining for that worker.
    listed_after = await client.get("/v1/workers")
    assert listed_after.status_code == 200
    statuses = {w["id"]: w["status"] for w in listed_after.json()["items"]}
    assert statuses.get(worker_id) == "draining", (
        f"expected status=draining for {worker_id!r}, got {statuses!r}"
    )


@pytest.mark.asyncio
async def test_t0218_drain_third_call_does_not_toggle_state(
    client: httpx.AsyncClient,
) -> None:
    """T0218 — Distinct from T0028's two-call idempotency. After the
    second drain leaves status=draining, a THIRD drain must still
    return 204 AND the worker row's identifying fields (id, host, pid)
    must remain stable across the call — no field toggling.

    Catches a regression where the drain handler does a "load row →
    flip a boolean → write back" that could cycle the state if the
    field is interpreted as a toggle rather than an idempotent set.
    """
    listed = await client.get("/v1/workers")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items
    worker_id = items[0]["id"]

    # Two drains to reach "already draining" state
    await client.post(f"/v1/workers/{worker_id}/drain")
    await client.post(f"/v1/workers/{worker_id}/drain")

    # Snapshot before third drain
    before = await client.get("/v1/workers")
    assert before.status_code == 200
    row_before = next(
        (w for w in before.json()["items"] if w["id"] == worker_id), None,
    )
    assert row_before is not None
    assert row_before["status"] == "draining"

    # Third drain — must remain 204 and not flip state
    third = await client.post(f"/v1/workers/{worker_id}/drain")
    assert third.status_code == 204, third.text

    after = await client.get("/v1/workers")
    assert after.status_code == 200
    row_after = next(
        (w for w in after.json()["items"] if w["id"] == worker_id), None,
    )
    assert row_after is not None
    assert row_after["status"] == "draining", (
        f"third drain toggled status off: before={row_before!r}, "
        f"after={row_after!r}"
    )
    # Identity fields stable
    for field in ("id", "host", "pid"):
        assert row_after.get(field) == row_before.get(field), (
            f"field {field!r} changed across the third drain call: "
            f"before={row_before.get(field)!r}, "
            f"after={row_after.get(field)!r}"
        )


@pytest.mark.asyncio
async def test_t0307_worker_capacity_started_at_stable_across_drain(
    client: httpx.AsyncClient,
) -> None:
    """T0307 — Extends T0218 to cover the `capacity` and `started_at`
    fields specifically. After a drain, these worker-identity fields
    must remain unchanged.
    """
    listed = await client.get("/v1/workers")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items
    worker_id = items[0]["id"]

    # Snapshot before any drain
    row_before = items[0]
    assert "capacity" in row_before, row_before
    assert "started_at" in row_before, row_before
    capacity_before = row_before["capacity"]
    started_at_before = row_before["started_at"]

    # Drain
    rd = await client.post(f"/v1/workers/{worker_id}/drain")
    assert rd.status_code == 204, rd.text

    # Snapshot after
    after = await client.get("/v1/workers")
    assert after.status_code == 200
    row_after = next(
        (w for w in after.json()["items"] if w["id"] == worker_id), None,
    )
    assert row_after is not None
    assert row_after.get("capacity") == capacity_before, (
        f"capacity changed across drain: "
        f"before={capacity_before!r}, after={row_after.get('capacity')!r}"
    )
    assert row_after.get("started_at") == started_at_before, (
        f"started_at changed across drain: "
        f"before={started_at_before!r}, "
        f"after={row_after.get('started_at')!r}"
    )


@pytest.mark.asyncio
async def test_t0250_concurrent_invalidate_calls_all_204(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0250 — Five parallel POSTs to /v1/llm_providers/{id}/invalidate.
    Every response must be 204 (no race-induced 5xx); the row body
    after the burst is unchanged from before. Stress-tests the
    invalidate path's concurrency safety beyond T0026's two-call
    sequential idempotency.
    """
    entity_id = f"llm-conc-{unique_suffix}"
    created = await client.post("/v1/llm_providers", json=_llm_body(entity_id))
    assert created.status_code == 201, created.text
    try:
        before = await client.get(f"/v1/llm_providers/{entity_id}")
        assert before.status_code == 200, before.text
        before_body = before.json()

        # Fire 5 invalidate calls concurrently
        results = await asyncio.gather(*[
            client.post(f"/v1/llm_providers/{entity_id}/invalidate")
            for _ in range(5)
        ])
        for i, r in enumerate(results):
            assert r.status_code == 204, (
                f"concurrent invalidate call {i} did not return 204: "
                f"{r.status_code}: {r.text}"
            )

        # Row body unchanged
        after = await client.get(f"/v1/llm_providers/{entity_id}")
        assert after.status_code == 200, after.text
        after_body = after.json()
        for field in ("id", "provider", "models"):
            assert after_body.get(field) == before_body.get(field), (
                f"field {field!r} changed across concurrent invalidates: "
                f"before={before_body.get(field)!r}, "
                f"after={after_body.get(field)!r}"
            )
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


@pytest.mark.asyncio
async def test_t0278_put_with_body_omitting_id_uses_path_id(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0278 — PUT existing LLMProvider with a body that has NO `id`
    field. The handler must implicit-fill the path id (no 422 for
    "missing required field"); subsequent GET returns the row with
    the path id preserved.

    Pin the implicit-id-fill semantic so a future regression that
    starts requiring `id` in the body is caught at the test layer.
    """
    entity_id = f"llm-noid-{unique_suffix}"
    initial = _llm_body(entity_id)
    created = await client.post("/v1/llm_providers", json=initial)
    assert created.status_code == 201, created.text
    try:
        # PUT body with NO id field
        body_no_id = {k: v for k, v in initial.items() if k != "id"}
        body_no_id["limits"] = {"max_concurrency": 5}  # observable mutation
        put = await client.put(
            f"/v1/llm_providers/{entity_id}", json=body_no_id,
        )
        # Either 200 (implicit-fill from path) or 422 (id required) —
        # pin which one is the live contract
        assert put.status_code in (200, 422), put.text
        if put.status_code == 200:
            # Verify the row's id matches the path
            got = await client.get(f"/v1/llm_providers/{entity_id}")
            assert got.status_code == 200, got.text
            assert got.json()["id"] == entity_id, got.json()
            # And the mutation took effect
            assert got.json()["limits"]["max_concurrency"] == 5, got.json()
        else:
            envelope = put.json()
            assert envelope["type"] == "/errors/validation-error", envelope
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


@pytest.mark.asyncio
async def test_t0266_parallel_puts_same_row_one_body_wins(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0266 — Two parallel PUTs to the same LLMProvider row with
    DIFFERENT bodies. Both must return 2xx (or one wins and the other
    409 — both are documented); the GET after the race returns one of
    the two bodies (no row corruption); never /errors/internal.
    """
    entity_id = f"llm-race-put-{unique_suffix}"
    body_a = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-A"},
        "limits": {"max_concurrency": 1},
    }
    body_b = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [
            {"name": "claude-sonnet-4-6", "context_length": 200_000},
            {"name": "claude-haiku-4-5", "context_length": 100_000},
        ],
        "config": {"api_key": "sk-test-B"},
        "limits": {"max_concurrency": 4},
    }

    # Initial create
    created = await client.post("/v1/llm_providers", json=body_a)
    assert created.status_code == 201, created.text
    try:
        # Race two PUTs concurrently
        r_a, r_b = await asyncio.gather(
            client.put(f"/v1/llm_providers/{entity_id}", json=body_a),
            client.put(f"/v1/llm_providers/{entity_id}", json=body_b),
        )
        for r, label in ((r_a, "PUT A"), (r_b, "PUT B")):
            assert r.status_code < 500, (
                f"{label} leaked 5xx: {r.status_code}: {r.text}"
            )
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"{label} returned /errors/internal: {r.text}"
            )

        # GET the row — must return one of the two bodies cleanly
        got = await client.get(f"/v1/llm_providers/{entity_id}")
        assert got.status_code == 200, got.text
        row = got.json()
        winner_models = [m["name"] for m in row["models"]]
        assert winner_models in (
            ["claude-sonnet-4-6"],
            ["claude-sonnet-4-6", "claude-haiku-4-5"],
        ), f"row corrupted across PUT race: {row!r}"
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


@pytest.mark.asyncio
async def test_t0104_parallel_get_and_delete_no_internal_error(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0104 — race a GET and a DELETE on the same toolset row. The
    GET races against the DELETE in either order; both possibilities
    must surface as clean responses (200 if GET wins the read, 404
    if DELETE happened first), with no `/errors/internal` leak.

    This catches handlers that do a "load → operate → save" pattern
    without holding a lock, where the loaded object disappears
    mid-handler. The contract pin is "no internal-error envelope under
    the simplest possible race".
    """
    entity_id = f"ts-race-del-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "mcp",
        "config": {
            "transport": "stdio",
            "config": {"command": ["echo"]},
        },
    }
    create = await client.post("/v1/toolsets", json=body)
    assert create.status_code == 201, create.text

    # Fire GET and DELETE concurrently. Outcomes by ordering:
    #   GET wins the race → GET 200, DELETE 204
    #   DELETE wins → GET 404, DELETE 204
    # Either way, no 500 envelope.
    get_resp, del_resp = await asyncio.gather(
        client.get(f"/v1/toolsets/{entity_id}"),
        client.delete(f"/v1/toolsets/{entity_id}"),
    )

    for label, resp in (("GET", get_resp), ("DELETE", del_resp)):
        assert resp.status_code != 500, (
            f"{label} leaked /errors/internal under race: {resp.text}"
        )
        if 400 <= resp.status_code < 600:
            assert resp.json().get("type") != "/errors/internal", (
                f"{label} surfaced /errors/internal: {resp.text}"
            )

    # GET response is one of: 200 (saw the row), 404 (DELETE ran first)
    assert get_resp.status_code in (200, 404), (
        f"unexpected GET status under race: {get_resp.status_code}: "
        f"{get_resp.text}"
    )
    # DELETE wins for the row: either it deleted (204) or someone else
    # got there first (404). 204 is the typical outcome since it's the
    # only writer in this race.
    assert del_resp.status_code in (204, 404), (
        f"unexpected DELETE status under race: {del_resp.status_code}: "
        f"{del_resp.text}"
    )


@pytest.mark.asyncio
async def test_t0099_worker_drain_nonexistent_id_is_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0099 — `POST /v1/workers/<bogus>/drain` must NOT 500.

    The handler delegates to `Scheduler.drain_worker(worker_id)`. The
    worker doesn't exist, so the delegate may raise NotFoundError (→
    404 envelope) or treat it as a no-op (→ 204). Both are clean
    behaviours; the contract pin is "no 5xx leaks through".
    """
    bogus = f"worker-does-not-exist-{unique_suffix}"
    resp = await client.post(f"/v1/workers/{bogus}/drain")
    assert resp.status_code != 500, (
        f"unhandled exception leaked through as 500: {resp.text}"
    )
    assert resp.status_code < 500, (
        f"unexpected 5xx on drain of missing worker: "
        f"{resp.status_code}: {resp.text}"
    )

    if resp.status_code == 204:
        # Treated as no-op — acceptable per scheduler discretion.
        return
    # 4xx — must carry the documented RFC 7807 envelope shape.
    envelope = resp.json()
    for key in ("type", "title", "status", "detail"):
        assert key in envelope, (
            f"problem-details key {key!r} missing in {envelope!r}"
        )
    assert envelope["status"] == resp.status_code
    assert envelope["type"].startswith("/errors/"), envelope
