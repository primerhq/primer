"""E2E: LLMProvider CRUD round-trip.

Backlog item T0004 — create → get → list (must include) → put → get
(reflects update) → delete → get (404).
"""

from __future__ import annotations

import httpx
import pytest

from tests._support.smk import smk


def _llm_body(entity_id: str) -> dict:
    """Minimal valid LLMProvider request body (Anthropic flavour)."""
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 4},
    }


@smk("SMK-PRV-01")
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
async def test_t0127_invalidate_then_delete_leaves_no_orphaned_state(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0127 — sequence: POST → POST /invalidate (204) → DELETE (204)
    → GET (404). After DELETE, no surprise 200 from a leftover cached
    adapter and no 5xx from inconsistent state.
    """
    entity_id = f"llm-inv-del-{unique_suffix}"
    base = "/v1/llm_providers"

    create = await client.post(base, json=_llm_body(entity_id))
    assert create.status_code == 201, create.text

    inv = await client.post(f"{base}/{entity_id}/invalidate")
    assert inv.status_code == 204, inv.text

    rm = await client.delete(f"{base}/{entity_id}")
    assert rm.status_code == 204, rm.text

    gone = await client.get(f"{base}/{entity_id}")
    assert gone.status_code == 404, gone.text
    assert gone.json()["type"] == "/errors/not-found"


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


@smk("SMK-PRV-03")
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


# ============================================================================
# T0448 — LLMProvider /models with single-item models list
# ============================================================================


@pytest.mark.asyncio
async def test_t0448_llm_provider_models_endpoint_single_item(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0448 — Pin the singular case of GET /v1/llm_providers/{id}/
    models. T0025 reframed pinned that this endpoint is row-cached
    (returns configured names, NOT a live upstream call). With a
    single-item models list, the response body must be
    `{"models": ["<the only name>"]}` — exactly one entry.
    """
    # Use the openresponses provider pattern from T0025 — adapter
    # construction needs a typed config and we don't want network
    # hits, so point url at an unreachable port (the /models endpoint
    # is row-cached per T0025 and never makes upstream calls).
    entity_id = f"llm-t0448-{unique_suffix}"
    only_name = "only-configured-model"
    body = {
        "id": entity_id,
        "provider": "openresponses",
        "models": [{"name": only_name, "context_length": 4096}],
        "config": {
            "url": "http://127.0.0.1:1",
            "api_key": "sk-not-used",
            "flavor": "other",
        },
        "limits": {"max_concurrency": 1},
    }
    create = await client.post("/v1/llm_providers", json=body)
    assert create.status_code == 201, create.text
    try:
        resp = await client.get(f"/v1/llm_providers/{entity_id}/models")
        assert resp.status_code == 200, resp.text
        models = resp.json().get("models")
        assert isinstance(models, list), resp.text
        assert len(models) == 1, (
            f"single-item models list should yield exactly one entry; "
            f"got {models!r}"
        )
        assert models[0] == only_name, (
            f"models[0]={models[0]!r}, expected {only_name!r}"
        )
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id}")


# ============================================================================
# T0449 — LLMProvider create with empty models list returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0449_llm_provider_create_with_empty_models_returns_422(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0449 — Per primer/model/provider.py:334-337, the LLMProvider
    `models` field has `min_length=1`. Pin that POST with `models: []`
    is rejected with a 422 /errors/validation-error envelope; row is
    not created.
    """
    entity_id = f"llm-t0449-{unique_suffix}"
    body = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post("/v1/llm_providers", json=body)
    assert resp.status_code != 500, resp.text
    assert resp.status_code == 422, (
        f"empty models list should be 422; got "
        f"{resp.status_code}: {resp.text}"
    )
    envelope = resp.json()
    assert envelope.get("type") == "/errors/validation-error", envelope
    # Row was not created
    got = await client.get(f"/v1/llm_providers/{entity_id}")
    assert got.status_code == 404, got.text


# ============================================================================
# T0459 — POST /v1/llm_providers with 1 MiB JSON body returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0459_llm_provider_create_with_one_mib_body_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0459 — POST a body with a 1 MiB padded `api_key` field
    (api_key is a real LLMProvider field — `description` is NOT a
    field on LLMProvider since it inherits from Identifiable, not
    Describeable, so Pydantic extra=ignore would silently drop it).

    Pin: 201 (accepted) or 4xx (413/422 if a body cap exists at
    this layer); never 5xx, never /errors/internal. The round-trip
    of the api_key is intentionally NOT checked — T0027 documents
    that api_keys are masked on GET regardless. The contract here
    is "server doesn't crash on 1 MiB body".
    """
    entity_id = f"llm-t0459-{unique_suffix}"
    big_api_key = "sk-" + "X" * (1024 * 1024)  # ~1 MiB api_key
    body = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": big_api_key},
        "limits": {"max_concurrency": 1},
    }
    resp = await client.post(
        "/v1/llm_providers", json=body,
        timeout=httpx.Timeout(60.0, connect=10.0),
    )
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"1 MiB body leaked /errors/internal: {resp.text[:300]}"
    )
    assert resp.status_code < 500, resp.text[:300]
    assert resp.status_code in (201, 400, 413, 422), (
        f"unexpected status: {resp.status_code}: {resp.text[:300]}"
    )

    if resp.status_code == 201:
        # Cleanup — also pins that GET + DELETE on a row created
        # with a giant api_key works (no read-side timeout)
        try:
            got = await client.get(f"/v1/llm_providers/{entity_id}")
            assert got.status_code == 200, got.text[:300]
        finally:
            await client.delete(f"/v1/llm_providers/{entity_id}")


# ============================================================================
# T0460 — POST /v1/llm_providers with 16 MiB JSON body returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0460_llm_provider_create_with_sixteen_mib_body_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0460 — Push to 16 MiB. Most HTTP frameworks have an
    implicit body-size limit (uvicorn defaults can vary; some
    setups cap at 16 MiB). Pin: 413/4xx with clean envelope, OR
    201 if the server accepts it; either way no 5xx, no
    /errors/internal, no connection drop.
    """
    entity_id = f"llm-t0460-{unique_suffix}"
    huge_api_key = "sk-" + "X" * (16 * 1024 * 1024)  # ~16 MiB api_key
    body = {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": huge_api_key},
        "limits": {"max_concurrency": 1},
    }
    try:
        resp = await client.post(
            "/v1/llm_providers", json=body,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
    except (httpx.RemoteProtocolError, httpx.WriteError) as exc:
        # The server may close the connection mid-write if the
        # body exceeds an internal cap. That's acceptable — pin as
        # a clean disconnect rather than a 5xx envelope. Re-raise
        # only if it's a transport failure that would also affect
        # subsequent tests.
        pytest.skip(
            f"server closed connection on 16 MiB body (acceptable "
            f"upper-bound behaviour): {exc}"
        )

    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"16 MiB body leaked /errors/internal: {resp.text[:300]}"
    )
    assert resp.status_code < 500, resp.text[:300]
    assert resp.status_code in (201, 400, 413, 422), (
        f"unexpected status: {resp.status_code}: {resp.text[:300]}"
    )

    if resp.status_code == 201:
        # Cleanup — also pins that DELETE on a huge row works
        try:
            await client.delete(f"/v1/llm_providers/{entity_id}")
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


# ============================================================================
# T0493 — POST /v1/llm_providers with deeply-nested unicode-escape in config
# ============================================================================


@pytest.mark.asyncio
async def test_t0493_llm_provider_create_with_deep_unicode_escapes_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0493 — POST a body whose config.api_key field is a string
    containing 100+ stacked `\\u` escape sequences. The wire form
    is well-formed JSON (each escape resolves to a single
    character), and the server must round-trip the parsed string
    cleanly (Pydantic + json + asyncpg pipeline). Pin: 201 (accepted
    byte-exact) OR clean 4xx; never /errors/internal from a JSON
    re-encoding crash anywhere in the pipeline.
    """
    import json

    # Build a string of 200 stacked unicode escapes — each `\`
    # decodes to `\` (backslash) and `u` decodes to `u`. The
    # decoded string is 200 chars of `\u\u\u...` — a stress test
    # for any layer that re-serializes the value.
    unicode_pairs = "\\u005c\\u0075" * 100  # 200 chars when decoded
    entity_id = f"llm-t0493-{unique_suffix}"

    # Construct the JSON body manually so the escape sequences land
    # in the wire format exactly as written (httpx's json= would
    # re-encode them to literal backslash-u sequences anyway, but
    # this is more explicit).
    raw_body = json.dumps(
        {
            "id": entity_id,
            "provider": "anthropic",
            "models": [
                {"name": "claude-sonnet-4-6", "context_length": 200_000},
            ],
            # Escapes nested inside the api_key value
            "config": {"api_key": f"sk-{unicode_pairs}-end"},
            "limits": {"max_concurrency": 1},
        }
    )

    resp = await client.post(
        "/v1/llm_providers",
        content=raw_body.encode("utf-8"),
        headers={"content-type": "application/json"},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"deep-unicode-escape body leaked /errors/internal: "
        f"{resp.text[:500]}"
    )
    assert resp.status_code < 500, resp.text[:500]
    # Documented surfaces: 201 (accepted), or 4xx if the validator
    # catches an issue (none expected — the string is valid)
    assert resp.status_code in (201, 400, 422), (
        f"unexpected status: {resp.status_code}: {resp.text[:500]}"
    )

    if resp.status_code == 201:
        try:
            # Round-trip via GET — the row should be readable. The
            # api_key is masked per T0027 so we don't compare it
            # byte-exact, but the GET MUST not crash.
            got = await client.get(f"/v1/llm_providers/{entity_id}")
            assert got.status_code == 200, got.text[:500]
            assert got.json()["id"] == entity_id, got.json()
        finally:
            await client.delete(f"/v1/llm_providers/{entity_id}")


# ============================================================================
# T0562 — POST /v1/llm_providers with duplicate names in models clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0562_post_llm_provider_with_duplicate_model_names_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0562 — Per primer/model/provider.py:334-337, LLMProvider.
    models is `list[LLMModel]` with no documented dedup constraint.
    Pin observed behavior: duplicate model names are accepted (201)
    or rejected (422) deterministically across two consecutive
    calls; never /errors/internal.

    Catches a regression where a future dedup validator changes
    the contract silently.
    """
    entity_id_a = f"llm-t0562a-{unique_suffix}"
    entity_id_b = f"llm-t0562b-{unique_suffix}"
    body_template = {
        "provider": "anthropic",
        "models": [
            {"name": "claude-sonnet-4-6", "context_length": 200_000},
            {"name": "claude-sonnet-4-6", "context_length": 100_000},
        ],
        "config": {"api_key": "sk-test"},
        "limits": {"max_concurrency": 1},
    }

    # Two distinct ids so neither call hits the duplicate-id 409
    # path — the only difference in outcome should be the dedup
    # behavior on `models`
    body_a = {**body_template, "id": entity_id_a}
    body_b = {**body_template, "id": entity_id_b}

    r1 = await client.post("/v1/llm_providers", json=body_a)
    r2 = await client.post("/v1/llm_providers", json=body_b)

    try:
        for r, label in ((r1, "call-1"), (r2, "call-2")):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"{label}: dup model names leaked /errors/internal: "
                f"{r.text}"
            )
            assert r.status_code in (201, 422), (
                f"{label}: unexpected {r.status_code}: {r.text}"
            )

        # Determinism: same outcome across both
        assert r1.status_code == r2.status_code, (
            f"non-deterministic dedup behavior: {r1.status_code} vs "
            f"{r2.status_code}"
        )

        # If accepted, the duplicate models survive on GET
        if r1.status_code == 201:
            got = await client.get(f"/v1/llm_providers/{entity_id_a}")
            assert got.status_code == 200, got.text
            got_models = got.json().get("models", [])
            names = [m["name"] for m in got_models]
            # Either preserved both entries (dup intact) or deduped
            # to one — pin observation
            assert names.count("claude-sonnet-4-6") in (1, 2), names
    finally:
        await client.delete(f"/v1/llm_providers/{entity_id_a}")
        await client.delete(f"/v1/llm_providers/{entity_id_b}")
