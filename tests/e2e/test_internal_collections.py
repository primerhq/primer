"""E2E: internal-collections subsystem gating.

Covers backlog item T0020 — when the subsystem hasn't been activated
(no config row), ``GET /v1/internal_collections/config`` returns 404
with the ``/errors/not-found`` envelope.

Bringup runs against a freshly-created database, so the config row is
guaranteed absent at the start of every iteration. This test does NOT
create the config (that would interfere with sibling tests that rely
on the subsystem being inactive); instead it only asserts the absence
behaviour.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0020_internal_collections_config_404_when_inactive(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/internal_collections/config")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["type"] == "/errors/not-found"
    assert body["status"] == 404
    # detail should point operators at the activation path.
    assert "PUT" in body["detail"] or "configure" in body["detail"].lower()


@pytest.mark.asyncio
async def test_t0019_search_503_subsystem_inactive(
    client: httpx.AsyncClient,
) -> None:
    """T0019 — `POST /v1/agents/search` returns 503 with the manually-set
    `/errors/subsystem-inactive` slug when the subsystem is not active.

    Bringup never activates the subsystem, so this test runs against the
    inactive state without any setup of its own.
    """
    body = {"query": "anything", "top_k": 5}
    resp = await client.post("/v1/agents/search", json=body)
    assert resp.status_code == 503, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/subsystem-inactive", envelope
    assert envelope["status"] == 503


@pytest.mark.asyncio
async def test_t0060_search_top_k_zero_rejected(
    client: httpx.AsyncClient,
) -> None:
    """T0060 — Pydantic validates SearchRequest.top_k (ge=1) before the
    handler runs, so top_k=0 yields 422 even when the subsystem is
    inactive."""
    resp = await client.post(
        "/v1/agents/search", json={"query": "x", "top_k": 0},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "/errors/validation-error", body
    assert body["status"] == 422


@pytest.mark.asyncio
async def test_t0061_search_top_k_above_cap_rejected(
    client: httpx.AsyncClient,
) -> None:
    """T0061 — top_k=101 is above the documented cap of 100 (le=100);
    Pydantic body validation rejects it with 422 regardless of
    subsystem state."""
    resp = await client.post(
        "/v1/agents/search", json={"query": "x", "top_k": 101},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "/errors/validation-error", body
    assert body["status"] == 422


@pytest.mark.asyncio
async def test_t0021_bootstrap_404_when_no_config(
    client: httpx.AsyncClient,
) -> None:
    """T0021 — `POST /v1/internal_collections/bootstrap` returns 404 with
    `/errors/not-found` when no config row exists.

    The handler raises ``NotFoundError("internal collections subsystem
    is not configured; PUT /v1/internal_collections/config first.")``
    which the registry maps to status 404, slug `/errors/not-found`.
    """
    resp = await client.post("/v1/internal_collections/bootstrap")
    assert resp.status_code == 404, resp.text
    envelope = resp.json()
    assert envelope["type"] == "/errors/not-found", envelope
    assert envelope["status"] == 404
    assert (
        "configured" in envelope["detail"].lower()
        or "PUT" in envelope["detail"]
    )


@pytest.mark.asyncio
async def test_t0166_graphs_and_tools_search_503_subsystem_inactive(
    client: httpx.AsyncClient,
) -> None:
    """T0166 — extends T0019's agent-side gating proof to the other two
    search routes. Both `/v1/graphs/search` and `/v1/tools/search` must
    return 503 with `/errors/subsystem-inactive` when the
    internal-collections subsystem hasn't been bootstrapped.

    `/v1/collections/search` is the fourth search route; T0019 already
    pins the agent route. Together with this test, three of the four
    search routes are explicitly gated. The collections route is
    expected to behave the same way; future iteration may add the
    fourth pin if needed.
    """
    body = {"query": "anything", "top_k": 5}
    for path in ("/v1/graphs/search", "/v1/tools/search"):
        resp = await client.post(path, json=body)
        assert resp.status_code == 503, (
            f"{path} expected 503 before bootstrap, got "
            f"{resp.status_code}: {resp.text}"
        )
        envelope = resp.json()
        assert envelope["type"] == "/errors/subsystem-inactive", (
            f"{path}: {envelope!r}"
        )
        assert envelope["status"] == 503


# ============================================================================
# T0318 — search top_k=100 (documented max) accepted before bootstrap
# ============================================================================


@pytest.mark.asyncio
async def test_t0318_search_top_k_at_documented_max_accepted(
    client: httpx.AsyncClient,
) -> None:
    """T0318 — Spec §11 / T0061 say top_k is bounded `1..100`. T0061
    rejects 101; T0062 covers top_k=1. This pins the upper boundary
    (100 itself) is accepted: the request passes Pydantic validation
    and reaches the subsystem check (returning 503 since the
    subsystem isn't bootstrapped in this test).

    The contract pin is "top_k=100 is NOT a 422" — distinguishing
    Pydantic body validation from subsystem-inactive gating.
    """
    resp = await client.post(
        "/v1/agents/search",
        json={"query": "anything", "top_k": 100},
    )
    # Pydantic accepts 100 → reaches subsystem-inactive check → 503
    assert resp.status_code == 503, (
        f"top_k=100 should reach the subsystem check (503), not be "
        f"rejected as 422: got {resp.status_code}: {resp.text}"
    )
    envelope = resp.json()
    assert envelope["type"] == "/errors/subsystem-inactive", envelope


# ============================================================================
# T0319 — search with very long query string returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0319_search_with_oversize_query_string_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0319 — POST /v1/agents/search with a 10000-character query
    string. Must produce a clean envelope (no /errors/internal): if
    the route accepts large queries, response is 503 (subsystem
    inactive) or 200 (if active); if there's a documented size cap,
    response is a clean 4xx.

    Catches a regression where a giant query string crashes the
    embedder or the SQL query builder.
    """
    huge_query = "x" * 10000
    resp = await client.post(
        "/v1/agents/search",
        json={"query": huge_query, "top_k": 5},
    )
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"oversize query leaked /errors/internal: {resp.text}"
    )
    # Acceptable codes: 503 (subsystem-inactive — most likely on
    # this iteration), 200 (subsystem active — unlikely without
    # bootstrap), or 4xx (size cap rejection)
    assert resp.status_code in (200, 422, 503) or 400 <= resp.status_code < 500, (
        f"unexpected status for oversize query: {resp.status_code}: "
        f"{resp.text}"
    )


# ============================================================================
# T0326 — DELETE /internal_collections/config before any PUT clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0326_ic_config_delete_before_put_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0326 — DELETE /v1/internal_collections/config when no config
    row exists. The handler must produce a clean envelope (404
    /errors/not-found OR 204 silent no-op like T0187 invalidate);
    NEVER /errors/internal.
    """
    resp = await client.delete("/v1/internal_collections/config")
    assert resp.status_code < 500, resp.text
    if resp.status_code >= 400:
        envelope = resp.json()
        assert envelope["type"].startswith("/errors/"), envelope
        assert envelope["type"] != "/errors/internal", envelope


# ============================================================================
# T0560 — /v1/agents/search query="   " (whitespace-only) clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0560_search_with_whitespace_only_query_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0560 — Search query="   " (three spaces, no other chars).
    Per the SearchRequest spec, `query` has min_length=1; whether
    the validator strips whitespace before checking is the unknown
    being pinned. Pin: clean envelope (422 if validator rejects
    whitespace-only OR 503 if it reaches subsystem-inactive OR 200
    with empty hits if subsystem is active and treats whitespace
    as no-op). Never /errors/internal.
    """
    resp = await client.post(
        "/v1/agents/search",
        json={"query": "   ", "top_k": 5},
    )
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"whitespace-only query leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code in (200, 422, 503), (
        f"unexpected status: {resp.status_code}: {resp.text}"
    )
    # If 200 (subsystem active), hits is a list; if 503 (inactive)
    # or 422 (rejected), envelope is cleanly typed
    if resp.status_code == 200:
        assert isinstance(resp.json().get("hits"), list), resp.text
    else:
        assert envelope.get("type", "").startswith("/errors/"), envelope


# ============================================================================
# T0587 — /v1/agents/search query="🚀🎉" (emoji-only) clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0587_search_with_emoji_only_query_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0587 — Sister of T0560 (whitespace-only query). Multi-byte
    UTF-8 emoji-only query string ("🚀🎉" = 2 codepoints, 8 bytes
    UTF-8 encoded). Some embedder pre-tokenisers strip non-ASCII
    aggressively and end up with empty input — pin that the route
    handles this with a clean envelope, never /errors/internal.

    Bringup never activates the subsystem, so the expected response
    is 503 /errors/subsystem-inactive. The hard pin is "no
    /errors/internal regardless of subsystem state" — even if a
    future iteration enables bootstrap, an emoji-only query must
    still produce a clean 200/4xx envelope.
    """
    resp = await client.post(
        "/v1/agents/search",
        json={"query": "🚀🎉", "top_k": 5},
    )
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"emoji-only query leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code in (200, 400, 422, 503), (
        f"unexpected status for emoji-only query: "
        f"{resp.status_code}: {resp.text}"
    )
    if resp.status_code == 200:
        assert isinstance(resp.json().get("hits"), list), resp.text
    else:
        assert envelope.get("type", "").startswith("/errors/"), envelope


# ============================================================================
# T0603 — IC config PUT with embedding_model="" returns 422
# ============================================================================


@pytest.mark.asyncio
async def test_t0603_ic_config_put_empty_embedding_model_clean_envelope(
    client: httpx.AsyncClient,
) -> None:
    """T0603 — Per matrix/api/routers/internal_collections.py:79-86,
    `embedding_model` has `min_length=1`. Pin: explicit empty string
    is rejected with 422 /errors/validation-error. The Pydantic
    body validator runs before the embedding-provider lookup, so this
    works against an inactive subsystem with no setup needed.

    Defence: subsequent GET still returns 404 (config row not created
    by the rejected PUT).
    """
    body = {
        "embedding_provider_id": "any-placeholder",
        "embedding_model": "",
    }
    resp = await client.put("/v1/internal_collections/config", json=body)
    envelope = resp.json() if resp.content else {}
    assert envelope.get("type") != "/errors/internal", (
        f"empty embedding_model leaked /errors/internal: {resp.text}"
    )
    assert resp.status_code == 422, (
        f"empty embedding_model should be 422 (Pydantic min_length=1); "
        f"got {resp.status_code}: {resp.text}"
    )
    assert envelope.get("type") == "/errors/validation-error", envelope

    # Defence: config row NOT created
    got = await client.get("/v1/internal_collections/config")
    assert got.status_code == 404, got.text


# ============================================================================
# T0612 — Two concurrent IC-config DELETEs converge cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0612_ic_config_concurrent_deletes_converge_clean(
    client: httpx.AsyncClient,
) -> None:
    """T0612 — Fire two concurrent DELETEs on /v1/internal_collections/
    config. Both must return clean envelopes (204 / 404) — never
    /errors/internal under config-DELETE race. Subsequent GET must
    return 404 (no config row).

    No bootstrap required: bringup leaves the subsystem inactive,
    so both DELETEs see "no row" — but the race is meaningful
    because the handler may set up backend cache state on either
    path.
    """
    import asyncio

    async def _del() -> httpx.Response:
        return await client.delete("/v1/internal_collections/config")

    r1, r2 = await asyncio.gather(_del(), _del(), return_exceptions=True)
    for i, r in enumerate((r1, r2)):
        assert not isinstance(r, BaseException), (
            f"DELETE #{i} raised: {r!r}"
        )
        envelope = r.json() if r.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"DELETE #{i} leaked /errors/internal: "
            f"{r.status_code}: {r.text}"
        )
        # 204 (no-op idempotent) or 404 (not found) — both acceptable
        assert r.status_code in (204, 404), (
            f"DELETE #{i} unexpected status: "
            f"{r.status_code}: {r.text}"
        )

    # Subsequent GET shows no config
    got = await client.get("/v1/internal_collections/config")
    assert got.status_code == 404, got.text


# ============================================================================
# T0707 — IC config PUT against deleted EmbeddingProvider clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0707_ic_config_put_against_deleted_embedding_provider(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0707 — Two-state pin: IC config write × provider lifecycle.
    Sequence:
        1. Create EmbeddingProvider X
        2. DELETE X
        3. PUT IC config referencing X

    The PUT must produce a clean envelope (4xx documented error or
    permissive 200 like T0265's Agent→deleted-provider pattern);
    never /errors/internal.

    Defence: If 200 (permissive path), DELETE the orphan config so
    it doesn't pollute later tests. If 4xx, no config row should exist.
    """
    embedder_id = f"emb-t0707-{unique_suffix}"
    pr = await client.post("/v1/embedding_providers", json={
        "id": embedder_id,
        "provider": "huggingface",
        "models": [
            {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
        ],
        "config": {"token": "hf-placeholder"},
        "limits": {"max_concurrency": 1},
    })
    assert pr.status_code == 201, pr.text

    # Ensure no IC config exists (defence against pollution)
    await client.delete("/v1/internal_collections/config")

    try:
        # DELETE the embedding provider
        rm = await client.delete(f"/v1/embedding_providers/{embedder_id}")
        assert rm.status_code in (204, 404), rm.text

        # PUT IC config referencing the deleted provider
        body = {
            "embedding_provider_id": embedder_id,
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        }
        resp = await client.put(
            "/v1/internal_collections/config", json=body,
        )
        envelope = resp.json() if resp.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"IC config PUT against deleted provider leaked "
            f"/errors/internal: {resp.text}"
        )
        # Acceptable: 200 (permissive — IC config persisted; bootstrap
        # would later fail to find provider), 4xx (validator caught
        # the dangling reference), or 502 (upstream surfaced).
        assert resp.status_code in (200, 400, 404, 422, 502), (
            f"IC config PUT against deleted provider unexpected "
            f"status: {resp.status_code}: {resp.text}"
        )
        if resp.status_code != 200:
            assert envelope["type"].startswith("/errors/"), envelope
    finally:
        # Clean up any IC config that landed
        await client.delete("/v1/internal_collections/config")
        # Provider already deleted; idempotent best-effort
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
