"""E2E: LLMProvider CRUD round-trip.

Backlog item T0004 — create → get → list (must include) → put → get
(reflects update) → delete → get (404).
"""

from __future__ import annotations

import httpx
import pytest


def _llm_body(entity_id: str) -> dict:
    """Minimal valid LLMProvider request body (Anthropic flavour)."""
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 4},
    }


@pytest.mark.asyncio
async def test_t0004_llm_provider_crud_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    entity_id = f"llm-{unique_suffix}"
    base = "/v1/llm_providers"
    body = _llm_body(entity_id)

    # --- create
    create = await client.post(base, json=body)
    assert create.status_code == 201, create.text
    assert create.json()["id"] == entity_id

    # --- get
    got = await client.get(f"{base}/{entity_id}")
    assert got.status_code == 200, got.text
    assert got.json()["id"] == entity_id

    # --- list must include
    listed = await client.get(f"{base}?limit=200&offset=0")
    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()["items"]]
    assert entity_id in ids, f"{entity_id!r} not in list response: {ids!r}"

    # --- put (update)
    updated = dict(body)
    updated["limits"] = {"max_concurrency": 16}
    put = await client.put(f"{base}/{entity_id}", json=updated)
    assert put.status_code == 200, put.text
    assert put.json()["limits"]["max_concurrency"] == 16

    # --- get reflects update
    got2 = await client.get(f"{base}/{entity_id}")
    assert got2.json()["limits"]["max_concurrency"] == 16

    # --- delete
    deleted = await client.delete(f"{base}/{entity_id}")
    assert deleted.status_code == 204, deleted.text

    # --- get after delete = 404
    gone = await client.get(f"{base}/{entity_id}")
    assert gone.status_code == 404
    assert gone.json()["type"] == "/errors/not-found"


@pytest.mark.asyncio
async def test_t0119_delete_then_recreate_same_id_returns_new_row(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0119 — sequence: POST id `X` v1 → DELETE → POST id `X` v2 →
    GET id `X`. Final GET must return the v2 body (never 410, never
    a stale v1 read from a leftover cache).
    """
    entity_id = f"llm-recreate-{unique_suffix}"
    base = "/v1/llm_providers"

    v1 = _llm_body(entity_id)
    v1["limits"]["max_concurrency"] = 4
    v2 = _llm_body(entity_id)
    v2["limits"]["max_concurrency"] = 16
    v2["models"][0] = {"name": "different-model", "context_length": 50_000}

    create1 = await client.post(base, json=v1)
    assert create1.status_code == 201, create1.text

    rm = await client.delete(f"{base}/{entity_id}")
    assert rm.status_code == 204, rm.text

    create2 = await client.post(base, json=v2)
    assert create2.status_code == 201, create2.text
    try:
        got = await client.get(f"{base}/{entity_id}")
        assert got.status_code == 200, got.text
        body = got.json()
        # Must reflect v2, NOT v1
        assert body["limits"]["max_concurrency"] == 16, body
        assert body["models"][0]["name"] == "different-model", body
    finally:
        await client.delete(f"{base}/{entity_id}")


@pytest.mark.asyncio
async def test_t0125_provider_with_10kib_description_round_trips(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0125 — pin the practical max-length contract. LLMProvider
    extends Identifiable (no description field), so this test uses
    a 10 KiB API key — that's the realistic "large blob" surface on
    LLMProvider.

    Either the row round-trips intact OR we get a clean 4xx envelope.
    No 500 leak.
    """
    entity_id = f"llm-large-{unique_suffix}"
    base = "/v1/llm_providers"
    big_key = "sk-" + "x" * (10 * 1024)

    body = _llm_body(entity_id)
    body["config"]["api_key"] = big_key
    create = await client.post(base, json=body)
    assert create.status_code != 500, create.text
    if create.status_code == 201:
        try:
            # Per T0027, secrets must NOT be echoed in plaintext —
            # but the row must persist and be readable. Just assert
            # the GET responds 200; T0027 already pins the masking.
            got = await client.get(f"{base}/{entity_id}")
            assert got.status_code == 200, got.text
            assert got.json()["id"] == entity_id
            # Must NOT echo the plaintext key
            assert big_key not in got.text, (
                "10 KiB plaintext api_key leaked through GET"
            )
        finally:
            await client.delete(f"{base}/{entity_id}")
    else:
        assert 400 <= create.status_code < 500, create.text
        envelope = create.json()
        assert envelope["type"].startswith("/errors/"), envelope


@pytest.mark.asyncio
async def test_t0098_crud_lookup_case_sensitive_on_id(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0098 — entity ids are case-sensitive. An LLMProvider created
    as `Foo<suffix>` is NOT findable via `foo<suffix>`."""
    cased_id = f"Foo-{unique_suffix}"
    base = "/v1/llm_providers"

    create = await client.post(base, json=_llm_body(cased_id))
    assert create.status_code == 201, create.text
    try:
        # Same case → 200
        same = await client.get(f"{base}/{cased_id}")
        assert same.status_code == 200, same.text
        assert same.json()["id"] == cased_id

        # Lower-cased → 404
        lower = await client.get(f"{base}/{cased_id.lower()}")
        assert lower.status_code == 404, (
            f"expected case-sensitive 404 on lowercase lookup, got "
            f"{lower.status_code}: {lower.text}"
        )
        assert lower.json()["type"] == "/errors/not-found"
    finally:
        await client.delete(f"{base}/{cased_id}")


@pytest.mark.asyncio
async def test_t0105_invalidate_does_not_delete_row(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0105 — `POST /llm_providers/{id}/invalidate` only drops the
    cached adapter; it must NOT remove the persisted row. Subsequent
    GET still returns 200 with the same id and config."""
    entity_id = f"llm-inv-row-{unique_suffix}"
    base = "/v1/llm_providers"

    create = await client.post(base, json=_llm_body(entity_id))
    assert create.status_code == 201, create.text
    try:
        # Capture the row pre-invalidate
        before = await client.get(f"{base}/{entity_id}")
        assert before.status_code == 200, before.text

        inv = await client.post(f"{base}/{entity_id}/invalidate")
        assert inv.status_code == 204, inv.text

        # Row must still exist with identical body
        after = await client.get(f"{base}/{entity_id}")
        assert after.status_code == 200, (
            f"invalidate appears to have deleted the row: {after.text}"
        )
        assert after.json()["id"] == entity_id
        # Compare the entire response body for stability — invalidate
        # should be a no-op as far as the persisted row is concerned.
        assert after.json() == before.json(), (
            "invalidate altered the persisted row (it should only "
            "drop the cached adapter)"
        )
    finally:
        await client.delete(f"{base}/{entity_id}")


@pytest.mark.asyncio
async def test_t0032_put_then_invalidate_reflects_update(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0032 — PUT an LLMProvider, then `/invalidate`, then GET. The
    GET must reflect the updated row (proof the cache was cleared on
    PUT or invalidate; either way the API view is consistent), and
    `/invalidate` itself must return 204.
    """
    entity_id = f"llm-cas-{unique_suffix}"
    base = "/v1/llm_providers"

    create = await client.post(base, json=_llm_body(entity_id))
    assert create.status_code == 201, create.text

    try:
        # Mutate something observable
        updated = _llm_body(entity_id)
        updated["limits"]["max_concurrency"] = 32
        put = await client.put(f"{base}/{entity_id}", json=updated)
        assert put.status_code == 200, put.text

        inv = await client.post(f"{base}/{entity_id}/invalidate")
        assert inv.status_code == 204, inv.text

        got = await client.get(f"{base}/{entity_id}")
        assert got.status_code == 200, got.text
        assert got.json()["limits"]["max_concurrency"] == 32
    finally:
        await client.delete(f"{base}/{entity_id}")
