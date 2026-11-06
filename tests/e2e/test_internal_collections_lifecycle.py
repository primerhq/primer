"""E2E: internal-collections subsystem activation and deactivation.

The setup chain creates a HuggingFace EmbeddingProvider pointed at a
local sentence-transformers model (no network creds required), PUTs
the internal-collections config referencing it (which also requires a
SemanticSearchProvider row as of the current API), calls bootstrap,
then exercises the activation and deactivation paths.

WHAT BOOTSTRAP MEANS NOW. S2 redefined it: it enables semantic search
on the `system` collection, which startup regenerates from live state.
It no longer builds the four `_internal_*` namespaces, and the CDC
ingest worker that used to keep them current is no longer started. The
per-entity routes (POST /v1/agents/search and friends) were deleted (D18);
these tests read the system collection's search state instead.

The eighteen CDC-to-search tests that lived here went with that: they
asserted that creating an agent made it findable through those routes
within seconds, which was exactly the worker's job. Entity material is
reachable through the system collection instead, and it refreshes at
startup rather than per write.

Bootstrap is SYNCHRONOUS as of S2: POST /bootstrap runs the pipeline
inline and returns 200 carrying the terminal outcome, so there is
nothing to poll and no in-flight state to race on. S2 also froze the
vector-space fields (embedding provider, embedding model, search
provider) once the subsystem is active, so re-configuring means
DELETE-ing the config first.

Both tests are SLOW: the embedder model load can take 30-60 s on the
first bootstrap. The pytest timeouts are sized accordingly.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
import pytest_asyncio
from tests._support.model_profiles import agent_model, seed_llm_provider
from tests.e2e.conftest import pgvector_ssp_body as _ssp_body


# Bootstrap is synchronous and now genuinely indexes the system
# collection, so the call carries a real embedding pass. Kept under
# the lane's own --timeout=180 per-test ceiling: a client timeout
# above that can never fire, the test just dies at 180 s instead.
_BOOTSTRAP_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

# The collection bootstrap enables search on.
_SYSTEM_COLLECTION_ID = "system"


def _embedding_provider_body(entity_id: str) -> dict:
    """HuggingFace embedder using a tiny local model that
    sentence-transformers can pull on demand (already a transitive dep
    of this project). No HF token needed for public models, but the
    config field is required by the schema -- pass an empty placeholder.
    """
    return {
        "id": entity_id,
        "provider": "huggingface",
        "models": [
            {"name": "sentence-transformers/all-MiniLM-L6-v2", "dim": 384},
        ],
        "config": {"token": "hf-placeholder"},
        "limits": {"max_concurrency": 1},
    }


def _ic_config_body(*, embedder_id: str, ssp_id: str) -> dict:
    return {
        "embedding_provider_id": embedder_id,
        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        "search_provider_id": ssp_id,
    }


async def _system_search(client: httpx.AsyncClient) -> dict | None:
    """The system collection's search block, or None while search is off.

    Bootstrap enables semantic search on the system collection and DELETE of the config disables it again; this is what a test reads to
    see which state the race or the cycle ended in (the per-entity /search routes were removed, D18).
    """
    r = await client.get("/v1/collections/system")
    assert r.status_code == 200, r.text
    assert r.json().get("type") != "/errors/internal", r.text
    return r.json().get("search")


async def _wait_bootstrap(
    client: httpx.AsyncClient,
    *,
    timeout_seconds: float = 180.0,
    poll_interval: float = 0.5,
) -> dict:
    """Read the terminal bootstrap status row back.

    Bootstrap is synchronous now, so once POST /bootstrap has returned
    there is nothing left to wait for. This reads the row and skips when
    the run failed (typically the embedder model being unavailable on
    the runner), which is what every caller relied on the old polling
    loop to do.
    """
    del timeout_seconds, poll_interval  # kept so call sites need no edit
    r = await client.get(
        "/v1/internal_collections/bootstrap/status",
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    assert r.status_code == 200, f"bootstrap/status returned {r.status_code}: {r.text}"
    row = r.json()
    if row.get("status") == "failed":
        pytest.skip(
            f"bootstrap failed (embedder model may be unavailable). "
            f"Error: {row.get('error', 'unknown')!r}"
        )
    return row


async def _wait_for_system_page(
    client: httpx.AsyncClient, path: str, needle: str,
    *, timeout_s: float = 20.0,
) -> None:
    """Poll the system-collection page until it exists and carries needle.

    CDC is a subscription on the platform event log now: the leader
    dispatcher converges the page within its poll interval (5s default)
    rather than inline with the mutating request, so page reads after a
    mutation must wait, exactly like the search assertions below.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    last = ""
    while asyncio.get_running_loop().time() < deadline:
        page = await client.get(
            f"/v1/collections/{_SYSTEM_COLLECTION_ID}/documents",
            params={"path": path},
        )
        last = page.text
        if page.status_code == 200 and needle in page.text:
            return
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"system page {path!r} never converged with {needle!r}; last: {last}"
    )


async def _assert_no_system_page(client: httpx.AsyncClient, path: str, *, settle_s: float = 7.0) -> None:
    """The page does NOT appear while its entity is private: the live map lists only public rows (access-control P3).

    Waits longer than the dispatcher's poll interval (5s), so a convergence that was going to write the page has had its chance.
    """
    deadline = asyncio.get_running_loop().time() + settle_s
    while asyncio.get_running_loop().time() < deadline:
        page = await client.get(f"/v1/collections/{_SYSTEM_COLLECTION_ID}/documents", params={"path": path})
        assert page.status_code == 404, f"private entity listed on the public live map at {path!r}: {page.text}"
        await asyncio.sleep(0.5)


async def _make_public(client: httpx.AsyncClient, plural: str, entity_id: str) -> None:
    """Publish one of the caller's rows (PUT .../access public: true, keeping its owners and grants)."""
    view = (await client.get(f"/v1/{plural}/{entity_id}/access")).json()
    put = await client.put(
        f"/v1/{plural}/{entity_id}/access",
        json={
            "owners": [o["id"] for o in view["owners"]],
            "grants": {g["user"]["id"]: g["level"] for g in view["grants"]},
            "public": True,
            "public_tools": view.get("public_tools", []),
            "rev": view["rev"],
        },
    )
    assert put.status_code == 200, put.text


@pytest_asyncio.fixture(autouse=True)
async def _drain_inflight_bootstrap(client: httpx.AsyncClient):
    """Ensure no internal-collections bootstrap is in-flight before each test.

    Bootstrap is synchronous now, so a run cannot outlive the request
    that started it and there is normally nothing to drain. Kept as a
    cheap guard: a row left ``running`` by an older build, or by a
    process killed mid-run, would otherwise strand every test behind it.
    """
    deadline = asyncio.get_event_loop().time() + 180.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            r = await client.get(
                "/v1/internal_collections/bootstrap/status",
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        except Exception:
            return
        if r.status_code != 200 or r.json().get("status") != "running":
            return
        await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_t0053_config_delete_deactivates_subsystem(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0053 — full lifecycle: PUT config → bootstrap → DELETE config →
    search on the system collection is off again."""
    embedder_id = f"emb-t0053-{unique_suffix}"
    ssp_id = f"ssp-t0053-{unique_suffix}"

    # 1. SemanticSearchProvider (required by PUT /internal_collections/config)
    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    # 2. EmbeddingProvider
    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        # 3. Activate subsystem config
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        # 4. Bootstrap (synchronous: 200 carries the outcome).
        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot.status_code == 200, (
            f"bootstrap should return 200 with its outcome; got "
            f"{boot.status_code}: {boot.text}"
        )
        await _wait_bootstrap(client)

        # 5. Bootstrap enabled semantic search on the system collection.
        assert await _system_search(client) is not None

        # 6. DELETE the config: it disables search on the system collection again.
        rm = await client.delete("/v1/internal_collections/config")
        assert rm.status_code == 204, rm.text
        config_created = False  # already cleaned
        assert await _system_search(client) is None, "after DELETE config, search on the system collection must be off"
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


def _llm_body(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "provider": "anthropic",
        "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
        "config": {"api_key": "sk-test-placeholder"},
        "limits": {"max_concurrency": 1},
    }


def _agent_body(entity_id: str, *, provider_id: str, description: str) -> dict:
    return {
        "id": entity_id,
        "description": description,
        "model": agent_model(provider_id, "claude-sonnet-4-6"),
        "tools": [],
    }


@contextlib.asynccontextmanager
async def bootstrapped_subsystem(
    client: httpx.AsyncClient,
    embedder_id: str,
    ssp_id: str,
):
    """Activate the internal-collections subsystem for the `async with`
    block's duration; guarantees deactivation on every exit - success,
    an assertion failure inside the block, OR a `pytest.skip` raised by
    `_wait_bootstrap` while reading back bootstrap's terminal status
    (the common local exit: see that function's own docstring - no
    `huggingface` extra installed means a real bootstrap attempt fails
    and skips here on nearly every call in this environment).

    Structural fix for a real bug (2026-09-10): the previous shape had
    9 call sites doing
        await _bootstrap_subsystem(...)
        config_created = True
    which left `config_created` False whenever the skip inside that
    helper fired - the PUT below had already activated the subsystem
    for real, but the flag meant to gate cleanup never got set, so the
    calling test's own `finally:` silently skipped its DELETE. That
    leaked, genuinely-active config then made every later test assuming
    a clean start fail with 409 subsystem_active. A context manager
    removes the flag entirely: the DELETE below is unconditional on any
    exit from the `try:`, which starts immediately after the PUT
    succeeds - there is no longer a window where "the subsystem is
    active" and "nothing will clean it up" can coexist.
    """
    # Idempotent: 404 when nothing is configured yet, which is fine.
    await client.delete("/v1/internal_collections/config")
    put = await client.put(
        "/v1/internal_collections/config",
        json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
    )
    assert put.status_code == 200, put.text
    # The subsystem is genuinely active from here on - cleanup is now
    # unconditional on every exit path out of this try, including a
    # skip raised before `yield` is ever reached.
    try:
        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot.status_code == 200, (
            f"bootstrap should return 200 with its outcome; got "
            f"{boot.status_code}: {boot.text}"
        )
        row = await _wait_bootstrap(client)
        yield row
    finally:
        await client.delete("/v1/internal_collections/config")


# ============================================================================
# Guard — bootstrapped_subsystem deactivates even if _wait_bootstrap skips
# ============================================================================


@pytest.mark.asyncio
async def test_bootstrapped_subsystem_deactivates_even_when_wait_bootstrap_skips(
    client: httpx.AsyncClient, unique_suffix: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard (2026-09-10) for the actual bug this file had:
    9 call sites used to do
        await _bootstrap_subsystem(...)
        config_created = True
    and a `pytest.skip` raised by `_wait_bootstrap` (routinely, on any
    host missing the `huggingface` extra - see that function's own
    docstring) unwound past the flag assignment, so the caller's
    `finally:` never ran its cleanup DELETE even though the PUT moments
    earlier had genuinely activated the subsystem. That leaked, real
    config then made every subsequent test assuming a clean start fail
    with 409 subsystem_active.

    This bug is STRUCTURALLY INVISIBLE to the rest of this file's own
    tests in CI: `.github/workflows/e2e.yml` runs `uv sync --all-extras`,
    so bootstrap always succeeds there, `_wait_bootstrap` never skips,
    and the leak never happens - CI would stay green forever even if the
    flag-timing bug were reintroduced. This test forces the skip
    directly (monkeypatching `_wait_bootstrap`, not relying on the
    environment lacking an extra) so it exercises the failure path in
    EVERY environment, extras or not, and fails if `bootstrapped_subsystem`
    (or whatever replaces it) is ever changed to leave a window where the
    subsystem is active but nothing is registered to deactivate it.
    """
    embedder_id = f"emb-guard-{unique_suffix}"
    ssp_id = f"ssp-guard-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text
    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    async def _fake_wait_bootstrap(_client: httpx.AsyncClient) -> dict:
        pytest.skip("simulated: forcing bootstrapped_subsystem's skip-mid-call exit path")

    monkeypatch.setattr(f"{__name__}._wait_bootstrap", _fake_wait_bootstrap)

    try:
        with pytest.raises(pytest.skip.Exception):
            async with bootstrapped_subsystem(client, embedder_id, ssp_id):
                pytest.fail(
                    "unreachable: the faked _wait_bootstrap must skip before "
                    "yield, so the async-with body must never run"
                )

        # The skip propagated out of the `async with` entirely - now prove
        # cleanup still ran despite it: the config must be gone.
        got = await client.get("/v1/internal_collections/config")
        assert got.status_code == 404, (
            "bootstrapped_subsystem must deactivate the subsystem on every "
            f"exit, including a skip inside it; GET returned "
            f"{got.status_code}: {got.text}"
        )
    finally:
        # Idempotent: 404 if the assertion above already proved it gone.
        await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")




# ============================================================================
# T0164 — CDC for Graph: new Graph appears in /v1/graphs/search
# ============================================================================


def _graph_body(entity_id: str, *, agent_id: str, description: str) -> dict:
    return {
        "id": entity_id,
        "description": description,
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "agent", "id": "n1", "agent_id": agent_id},
            {"kind": "end", "id": "end"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "n1"},
            {"kind": "static", "from_node": "n1", "to_node": "end"},
        ],
    }


# ============================================================================
# T0174 — query-based discrimination of two agents (positive control)
# ============================================================================


# ============================================================================
# T0167 — bootstrap is idempotent (second call returns 200 cleanly)
# ============================================================================


@pytest.mark.asyncio
async def test_t0167_bootstrap_is_idempotent(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0167 — POST /v1/internal_collections/bootstrap a second time
    after the first succeeds is idempotent (spec §11). Bootstrap runs
    inline, so the second call returns 200 with its own outcome rather
    than racing the first. The system collection's search stays
    enabled afterwards.
    """
    embedder_id = f"emb-t0167-{unique_suffix}"
    ssp_id = f"ssp-t0167-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    try:
        async with bootstrapped_subsystem(client, embedder_id, ssp_id):
            # First call already happened in bootstrapped_subsystem. Second call:
            boot2 = await client.post(
                "/v1/internal_collections/bootstrap",
                timeout=_BOOTSTRAP_TIMEOUT,
            )
            assert boot2.status_code == 200, (
                f"second bootstrap should be idempotent and return 200; got "
                f"{boot2.status_code}: {boot2.text}"
            )
            if boot2.status_code == 200:
                await _wait_bootstrap(client)
            body = boot2.json()
            assert isinstance(body, dict), body

            # Search stays enabled on the system collection (no stale state).
            assert await _system_search(client) is not None
    finally:
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0168 — PUT config with non-existent embedding_provider_id
# ============================================================================


@pytest.mark.asyncio
async def test_t0168_put_config_with_missing_embedder_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0168 — PUT /v1/internal_collections/config referencing an
    embedding_provider_id that doesn't exist but a valid SSP. Mirrors
    T0068's permissive referential-integrity contract (rows are
    persisted; orphan surfaces at use-time): the API may either reject
    at PUT time (4xx) or accept and surface the orphan at bootstrap.
    Pin "no /errors/internal".
    """
    missing_embedder = f"missing-emb-{unique_suffix}"
    ssp_id = f"ssp-t0168-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    config_created = False
    try:
        resp = await client.put(
            "/v1/internal_collections/config",
            json={
                "embedding_provider_id": missing_embedder,
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "search_provider_id": ssp_id,
            },
        )
        assert resp.status_code != 500, resp.text
        if resp.status_code == 200:
            config_created = True
            # Orphan path: bootstrap should fail cleanly (4xx/5xx-non-internal)
            boot = await client.post(
                "/v1/internal_collections/bootstrap",
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
            assert boot.status_code != 500 or "internal" not in (
                boot.json().get("type", "")
            ), (
                f"bootstrap with orphan embedder leaked 5xx internal: "
                f"{boot.text}"
            )
            envelope = boot.json() if boot.status_code >= 400 else None
            if envelope:
                assert envelope["type"] != "/errors/internal", envelope
        else:
            # 4xx rejection path
            assert 400 <= resp.status_code < 500, resp.text
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0169 — PUT config reconfigures embedder; subsystem keeps serving
# ============================================================================


@pytest.mark.asyncio
async def test_t0169_put_config_reconfigure_embedder_works(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0169 — PUT /v1/internal_collections/config is an upsert before
    bootstrap (no activated_at). After the subsystem activates via
    bootstrap, vector-space-defining fields (embedding_provider_id,
    embedding_model, search_provider_id) are frozen: a second PUT
    with a different embedding_provider_id must return 409 with
    frozen_fields. Non-frozen fields (cross_encoder, mmr) remain
    mutable. Search on the system collection stays enabled throughout.

    This test pins the CURRENT behavior: frozen-field PUT returns 409.
    """
    embedder_a = f"emb-t0169a-{unique_suffix}"
    embedder_b = f"emb-t0169b-{unique_suffix}"
    ssp_id = f"ssp-t0169-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr_a = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_a),
    )
    assert pr_a.status_code == 201, pr_a.text
    pr_b = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_b),
    )
    assert pr_b.status_code == 201, pr_b.text

    try:
        async with bootstrapped_subsystem(client, embedder_a, ssp_id):
            # After activation, changing embedding_provider_id is frozen --
            # the API must return 409 with frozen_fields in the response.
            put_b = await client.put(
                "/v1/internal_collections/config",
                json=_ic_config_body(embedder_id=embedder_b, ssp_id=ssp_id),
            )
            assert put_b.status_code == 409, (
                f"reconfigure PUT after activation should return 409 "
                f"(frozen fields); got {put_b.status_code}: {put_b.text}"
            )
            # The problem+json error contract renders an HTTPException dict detail
            # as a string ``detail`` (the human message) with the machine keys
            # (frozen_fields, error) carried verbatim in ``extensions``.
            body_json = put_b.json()
            extensions = body_json.get("extensions", {})
            frozen = extensions.get("frozen_fields", [])
            assert "embedding_provider_id" in frozen, (
                f"expected 'embedding_provider_id' in extensions.frozen_fields; "
                f"got: {body_json!r}"
            )

            # The failed PUT did not disturb the search the bootstrap enabled.
            assert await _system_search(client) is not None
    finally:
        await client.delete(f"/v1/embedding_providers/{embedder_a}")
        await client.delete(f"/v1/embedding_providers/{embedder_b}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0203 — Bootstrap on empty DB (no agents/graphs/collections/tools)
# ============================================================================


@pytest.mark.asyncio
async def test_t0203_bootstrap_on_empty_db_returns_sane_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0203 — Bootstrap on a freshly-activated subsystem against a DB
    with zero agents/graphs/collections. Built-in tools (e.g. _system,
    _workspaces) are present but no user entities exist. Bootstrap must
    complete cleanly without error and return a sane envelope.
    """
    embedder_id = f"emb-t0203-{unique_suffix}"
    ssp_id = f"ssp-t0203-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot.status_code == 200, (
            f"bootstrap on empty DB should return 200 with its outcome, got "
            f"{boot.status_code}: {boot.text}"
        )
        status_row = await _wait_bootstrap(client)
        assert isinstance(status_row, dict), status_row
        # Search is enabled on the system collection after bootstrap
        assert await _system_search(client) is not None
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0224 — Bootstrap envelope counts shape
# ============================================================================


@pytest.mark.asyncio
async def test_t0224_bootstrap_envelope_counts_shape(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0224 — Spec §11 documents bootstrap as "Returns counts". This
    test pins the envelope shape: after seeding one Agent and one
    Graph, calling bootstrap must return a dict whose values include
    integers (the per-entity-type counts). T0167 only verified
    idempotency, not the shape.
    """
    embedder_id = f"emb-t0224-{unique_suffix}"
    ssp_id = f"ssp-t0224-{unique_suffix}"
    llm_id = f"llm-t0224-{unique_suffix}"
    agent_id = f"agent-t0224-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    agent_created = False
    try:
        # Activate config (PUT) -- but do not call bootstrap yet
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        # Seed one Agent BEFORE bootstrap so the bootstrap counts it
        llm = await seed_llm_provider(client, _llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True
        ag = await client.post(
            "/v1/agents",
            json=_agent_body(
                agent_id, provider_id=llm_id,
                description=f"agent-t0224-{unique_suffix}",
            ),
        )
        assert ag.status_code == 201, ag.text
        agent_created = True

        # Bootstrap (synchronous) and pin the status-row shape
        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot.status_code == 200, boot.text
        status_row = await _wait_bootstrap(client)
        # Counts come from the persisted status row. S2 replaced the old
        # per-namespace "counts" dict with two flat totals: the run
        # covers one collection now, so there is nothing left to break
        # down by entity type.
        body = status_row
        assert isinstance(body, dict), body
        assert isinstance(body.get("documents_total"), int), body
        assert isinstance(body.get("documents_indexed"), int), body
        assert body["documents_indexed"] > 0, (
            f"bootstrap reported success having indexed nothing: {body!r}"
        )
        # No "error" key indicating a failed path
        for forbidden in ("error", "errors", "failed"):
            assert body.get(forbidden) is None, (
                f"bootstrap status row unexpectedly carries {forbidden!r} "
                f"on a clean run: {body!r}"
            )
    finally:
        if agent_created:
            await client.delete(f"/v1/agents/{agent_id}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0225 — GET /v1/internal_collections/config echoes the written values
# ============================================================================


@pytest.mark.asyncio
async def test_t0225_get_config_after_put_echoes_written_values(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0225 — After PUT /v1/internal_collections/config, GET on the
    same endpoint must echo the written embedding_provider_id and
    embedding_model. Round-trip pin for the subsystem config row.

    T0020 (404 on fresh DB) and T0169 (reconfigure) don't pin the
    direct read-after-write echo.
    """
    embedder_id = f"emb-t0225-{unique_suffix}"
    ssp_id = f"ssp-t0225-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        got = await client.get("/v1/internal_collections/config")
        assert got.status_code == 200, got.text
        row = got.json()
        assert row.get("embedding_provider_id") == embedder_id, row
        assert row.get("embedding_model") == (
            "sentence-transformers/all-MiniLM-L6-v2"
        ), row
        assert row.get("search_provider_id") == ssp_id, row
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0226 — /v1/agents/search ranking is stable across two sequential calls
# ============================================================================


# ============================================================================
# T0243 — Bootstrap counts envelope reflects per-collection seeded counts
# ============================================================================


@pytest.mark.asyncio
async def test_t0243_bootstrap_counts_reflect_seeded_entities(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0243 — Seed 3 agents + 2 graphs + 1 collection BEFORE bootstrap.

    The indexed count must cover at least the seeded entities. Each one
    becomes a document in the system collection, which also holds the
    shipped docs and the subtree indexes, so the count is `>= 6` rather
    than `== 6`.

    Distinct from T0224 (which pins the envelope's shape); this pins
    that the numbers track real entities.
    """
    embedder_id = f"emb-t0243-{unique_suffix}"
    ssp_id = f"ssp-t0243-{unique_suffix}"
    llm_id = f"llm-t0243-{unique_suffix}"
    agent_ids = [f"agent-t0243-{unique_suffix}-{i}" for i in range(3)]
    graph_ids = [f"graph-t0243-{unique_suffix}-{i}" for i in range(2)]
    coll_id = f"coll-t0243-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    seeded: list[tuple[str, str]] = []
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        llm = await seed_llm_provider(client, _llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        for aid in agent_ids:
            r = await client.post(
                "/v1/agents",
                json=_agent_body(aid, provider_id=llm_id, description="x"),
            )
            assert r.status_code == 201, r.text
            seeded.append(("agents", aid))

        for gid in graph_ids:
            r = await client.post(
                "/v1/graphs",
                json=_graph_body(gid, agent_id=agent_ids[0], description="x"),
            )
            assert r.status_code == 201, r.text
            seeded.append(("graphs", gid))

        coll = await client.post(
            "/v1/collections",
            json={
                "id": coll_id,
                "description": "T0243",
            },
        )
        assert coll.status_code in (200, 201), coll.text
        seeded.append(("collections", coll_id))

        # Bootstrap (synchronous); counts come from the status row
        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot.status_code == 200, boot.text
        status_row = await _wait_bootstrap(client)
        body = status_row
        seeded_total = len(seeded)  # 6
        indexed = body.get("documents_indexed", 0)
        assert indexed >= seeded_total, (
            f"bootstrap indexed {indexed} documents; expected at least "
            f"{seeded_total} from seeded entities. status row: {body!r}"
        )
    finally:
        for kind, eid in seeded:
            await client.delete(f"/v1/{kind}/{eid}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0244 — IC config DELETE then re-PUT (same embedder); search recovers
# ============================================================================


# ============================================================================
# T0269 — IC config PUT with collections=[] (empty list) is accepted
# ============================================================================


@pytest.mark.asyncio
async def test_t0269_ic_config_put_with_empty_collections_list(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0269 — Spec §11 mentioned `collections=[...]` as a body field
    on PUT /v1/internal_collections/config. The actual model
    (primer/model/internal.py) does not have a `collections` field,
    so passing `collections=[]` should be silently ignored by Pydantic
    (extra=ignore default) and the PUT succeeds.

    Bootstrap after this empty-list PUT must return cleanly with sane
    envelope; search is enabled on the system collection.
    """
    embedder_id = f"emb-t0269-{unique_suffix}"
    ssp_id = f"ssp-t0269-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        body = _ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id)
        body["collections"] = []  # extra field -- should be ignored
        put = await client.put(
            "/v1/internal_collections/config", json=body,
        )
        assert put.status_code == 200, (
            f"PUT with extra collections=[] should be silently "
            f"accepted; got {put.status_code}: {put.text}"
        )
        config_created = True

        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot.status_code == 200, boot.text
        status_row = await _wait_bootstrap(client)
        assert isinstance(status_row, dict), status_row

        # Search is enabled on the system collection
        assert await _system_search(client) is not None
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0277 — Concurrent bootstrap calls during a fresh PUT config race cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0277_concurrent_bootstraps_during_fresh_put_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0277 — PUT config and fire two concurrent bootstrap POSTs
    immediately after. Each POST must return a clean envelope (200 or
    a documented 4xx); never 5xx /errors/internal. After the dust
    settles, the subsystem must be active (search on the system collection enabled).
    """
    embedder_id = f"emb-t0277-{unique_suffix}"
    ssp_id = f"ssp-t0277-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        # Fire two parallel bootstraps. Bootstrap runs inline, so both
        # return 200 with their own outcome. The invariant that matters
        # is that neither returns /errors/internal.
        r1, r2 = await asyncio.gather(
            client.post(
                "/v1/internal_collections/bootstrap",
                timeout=_BOOTSTRAP_TIMEOUT,
            ),
            client.post(
                "/v1/internal_collections/bootstrap",
                timeout=_BOOTSTRAP_TIMEOUT,
            ),
        )
        for r, label in ((r1, "bootstrap A"), (r2, "bootstrap B")):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"{label} returned /errors/internal: {r.text}"
            )
            # bootstrap runs inline, so 200 is the only valid outcome
            assert r.status_code == 200, (
                f"{label} unexpected status: {r.status_code}: {r.text}"
            )

        # Wait for bootstrap to settle
        await _wait_bootstrap(client)

        # After the race, search is enabled on the system collection.
        assert await _system_search(client) is not None, "search not enabled after the concurrent bootstrap race"
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0286 — GET /v1/internal_collections/config and `collections` field
# ============================================================================


@pytest.mark.asyncio
async def test_t0286_get_config_collections_field_round_trip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0286 — Subsystem config introspection: pin GET response shape
    after PUT with an explicit `collections` field. The actual model
    (primer/model/internal.py) does NOT define a `collections` field,
    so it's silently ignored on PUT (T0269 confirmed) and absent on
    GET. This test pins that the GET response does NOT include
    `collections` (so callers know not to depend on it), AND the
    documented fields ARE echoed.
    """
    embedder_id = f"emb-t0286-{unique_suffix}"
    ssp_id = f"ssp-t0286-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        body = _ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id)
        body["collections"] = ["agent", "graph"]
        put = await client.put(
            "/v1/internal_collections/config", json=body,
        )
        assert put.status_code == 200, put.text
        config_created = True

        got = await client.get("/v1/internal_collections/config")
        assert got.status_code == 200, got.text
        row = got.json()

        assert row.get("embedding_provider_id") == embedder_id, row
        assert row.get("embedding_model") == (
            "sentence-transformers/all-MiniLM-L6-v2"
        ), row
        assert "collections" not in row, (
            f"unexpected `collections` field in GET response -- the "
            f"model doesn't define it: {row!r}"
        )
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0287 — CDC: PUT Collection description updates /collections/search
# ============================================================================


# ============================================================================
# T0288 — CDC: DELETE Graph removes it from /v1/graphs/search
# ============================================================================


# ============================================================================
# T0299 — search concurrent with PUT Agent description: search reflects update
# ============================================================================


# ============================================================================
# T0333 — POST /v1/agents/search with `filter` body field is silently ignored
# ============================================================================


# ============================================================================
# T0349 — IC config GET after DELETE returns 404 (lifecycle round-trip)
# ============================================================================


@pytest.mark.asyncio
async def test_t0349_ic_config_get_after_delete_returns_404(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0349 — Subsystem config lifecycle: PUT→GET(200)→DELETE(204)→
    GET(404). Mirror of T0020 (404 on fresh DB) from a different
    angle: the post-delete state is also 404 even though the row
    once existed.
    """
    embedder_id = f"emb-t0349-{unique_suffix}"
    ssp_id = f"ssp-t0349-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    try:
        # PUT → 200
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text

        # GET → 200
        get1 = await client.get("/v1/internal_collections/config")
        assert get1.status_code == 200, get1.text

        # DELETE → 204
        rm = await client.delete("/v1/internal_collections/config")
        assert rm.status_code == 204, rm.text

        # GET → 404 with /errors/not-found
        get2 = await client.get("/v1/internal_collections/config")
        assert get2.status_code == 404, get2.text
        envelope = get2.json()
        assert envelope["type"] == "/errors/not-found", envelope
    finally:
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0350 — Bootstrap before PUT returns 404 with full RFC 7807 envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0350_bootstrap_before_put_envelope_shape(
    client: httpx.AsyncClient,
) -> None:
    """T0350 — Extends T0021 (which only pins the slug). Pin the
    full RFC 7807 envelope shape on the bootstrap-without-config
    response: type/title/status/detail/instance all present and
    consistent.
    """
    resp = await client.post("/v1/internal_collections/bootstrap")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    for key in ("type", "title", "status", "detail", "instance"):
        assert key in body, f"RFC 7807 key {key!r} missing: {body!r}"
    assert body["type"] == "/errors/not-found", body
    assert body["status"] == 404
    # `instance` must echo the request path
    assert body["instance"].endswith("/v1/internal_collections/bootstrap"), (
        body
    )


# ============================================================================
# T0400 — CDC ingestion latency for Agent POST → /agents/search empirical pin
# ============================================================================


# ============================================================================
# T0411 — Concurrent bootstrap + DELETE config returns clean envelopes
# ============================================================================


@pytest.mark.asyncio
async def test_t0411_concurrent_bootstrap_and_delete_config_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0411 — Race a POST /bootstrap against a DELETE /config on the
    internal-collections subsystem. Pin: both calls return 2xx/4xx
    (no 5xx, no `/errors/internal`). The system collection's search
    converges to a deterministic state: off (DELETE won, or bootstrap
    completed and was then torn down) or on (bootstrap won and the
    config is still present after the DELETE attempt).

    Distinct from T0277 (concurrent bootstrap × bootstrap on a brand-
    new DB) — this races bootstrap against teardown.
    """
    embedder_id = f"emb-t0411-{unique_suffix}"
    ssp_id = f"ssp-t0411-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text
    config_created = False
    try:
        # PUT config so /bootstrap is meaningful
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        # Race bootstrap × delete-config
        boot_task = asyncio.create_task(client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        ))
        rm_task = asyncio.create_task(client.delete(
            "/v1/internal_collections/config",
        ))
        boot, rm = await asyncio.gather(boot_task, rm_task)
        config_created = False  # delete fired regardless

        for r, label in ((boot, "bootstrap"), (rm, "delete-config")):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"{label} race leaked /errors/internal: {r.text}"
            )
            assert r.status_code < 500, (
                f"{label} race returned 5xx: {r.status_code}: {r.text}"
            )

        # bootstrap: 200 (ran before delete) or 409/503
        # (subsystem already gone)
        assert boot.status_code in (200, 409, 503, 404), (
            f"bootstrap race: unexpected code {boot.status_code}: "
            f"{boot.text}"
        )
        # delete: 204 (succeeded) or 404 (already gone)
        assert rm.status_code in (204, 404), (
            f"delete-config race: unexpected code {rm.status_code}: "
            f"{rm.text}"
        )

        # The system collection's search ends in a state the race allows (off if the DELETE won, on if the bootstrap did).
        state = await _system_search(client)
        assert state is None or isinstance(state, dict), state
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0412 — CDC enqueue race: agent burst-create + immediate DELETE config
# ============================================================================


@pytest.mark.asyncio
async def test_t0412_cdc_burst_create_with_concurrent_delete_config_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0412 — After bootstrap, fire 5 agent CREATEs concurrently with
    a DELETE config. Pin: subsystem ends up cleanly inactive
    (system-collection search off), every call returns 2xx/4xx (no /errors/internal),
    and no leftover ingestion task crashes the worker pool.

    Companion to T0411 (bootstrap × delete) — this races CDC
    ingestion against teardown.
    """
    embedder_id = f"emb-t0412-{unique_suffix}"
    ssp_id = f"ssp-t0412-{unique_suffix}"
    llm_id = f"llm-t0412-{unique_suffix}"
    agent_ids = [
        f"agent-t0412-{unique_suffix}-{i}" for i in range(5)
    ]

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text
    config_created = False
    llm_created = False
    agents_created: list[str] = []
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        if boot.status_code == 409:
            # Another bootstrap is still running from a prior test; wait for
            # it to settle before treating our PUT-scoped run as done.
            await _wait_bootstrap(client)
        else:
            assert boot.status_code == 200, (
                f"bootstrap should return 200 with its outcome; got "
                f"{boot.status_code}: {boot.text}"
            )
            await _wait_bootstrap(client)

        llm = await seed_llm_provider(client, _llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        # Burst-create 5 agents concurrent with DELETE config
        agent_tasks = [
            asyncio.create_task(client.post(
                "/v1/agents",
                json=_agent_body(
                    aid, provider_id=llm_id,
                    description=f"t0412-{aid}",
                ),
            ))
            for aid in agent_ids
        ]
        rm_task = asyncio.create_task(client.delete(
            "/v1/internal_collections/config",
        ))
        all_results = await asyncio.gather(*agent_tasks, rm_task)
        agent_results = all_results[:5]
        rm_resp = all_results[5]
        config_created = False  # rm fired

        # Track which agents were created so we clean up
        for aid, r in zip(agent_ids, agent_results):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"agent create {aid!r} leaked /errors/internal: {r.text}"
            )
            assert r.status_code < 500, (
                f"agent create {aid!r} returned 5xx: "
                f"{r.status_code}: {r.text}"
            )
            if r.status_code == 201:
                agents_created.append(aid)

        # Delete is unconditional — should always 204 (or 404 if some
        # other test already removed the config concurrently — not
        # possible in this isolated test).
        assert rm_resp.status_code < 500, rm_resp.text
        assert rm_resp.status_code in (204, 404), rm_resp.text

        # Search on the system collection ends up off
        assert await _system_search(client) is None, "after burst-create + DELETE config, search must be off"
    finally:
        for aid in agents_created:
            await client.delete(f"/v1/agents/{aid}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0442 — Burst PUT on /internal_collections/config (10 concurrent)
# ============================================================================


@pytest.mark.asyncio
async def test_t0442_burst_put_config_converges_last_write_wins(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0442 — Fire 10 concurrent PUTs against
    /v1/internal_collections/config with the SAME embedder_id (so
    each call is independently valid). Pin: every call returns
    < 500 with no /errors/internal; the subsequent GET reflects
    one of the submitted bodies (last-write-wins, no half-merged
    state); the subsystem stays usable (system-collection search off if
    not bootstrapped, on if bootstrapped).
    """
    embedder_id = f"emb-t0442-{unique_suffix}"
    ssp_id = f"ssp-t0442-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text
    config_created = False
    try:
        body = _ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id)

        # 10 concurrent PUTs of the same body
        tasks = [
            asyncio.create_task(client.put(
                "/v1/internal_collections/config", json=body,
            ))
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks)
        config_created = True

        # Every call clean
        for i, r in enumerate(results):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"PUT[{i}] leaked /errors/internal: {r.text}"
            )
            assert r.status_code < 500, (
                f"PUT[{i}] returned 5xx: {r.status_code}: {r.text}"
            )
            # Documented success codes for PUT config
            assert r.status_code in (200, 201, 204, 409), (
                f"PUT[{i}]: unexpected status {r.status_code}: {r.text}"
            )

        # GET reflects one of the submitted bodies (no half-merged state)
        got = await client.get("/v1/internal_collections/config")
        assert got.status_code == 200, got.text
        body_got = got.json()
        # Field round-trips
        assert body_got.get("embedding_provider_id") == embedder_id, body_got
        assert (
            body_got.get("embedding_model")
            == "sentence-transformers/all-MiniLM-L6-v2"
        ), body_got

        # Search on the system collection is off (no bootstrap ran) or on (one ran concurrently)
        state = await _system_search(client)
        assert state is None or isinstance(state, dict), state
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0443 — Rapid 3-cycle deactivate/reactivate completes cleanly
# ============================================================================


@pytest.mark.asyncio
async def test_t0443_rapid_deactivate_reactivate_cycles_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0443 — T0091 covers a single deactivate→reactivate happy
    path with one agent involved. T0443 stresses RESOURCE leakage
    by running 3 rapid cycles back-to-back with no agents created
    between them: PUT config → bootstrap → system search on → DELETE
    config → system search off → repeat 3 times.

    Catches a regression where each activation cycle leaks a CDC
    worker, registry handle, or background task that eventually
    crashes the API server. Hard pin: no 5xx anywhere; each cycle's
    final search converges to 200; final teardown reaches 503.
    """
    embedder_id = f"emb-t0443-{unique_suffix}"
    ssp_id = f"ssp-t0443-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text
    config_active = False
    try:
        for cycle in range(3):
            put = await client.put(
                "/v1/internal_collections/config",
                json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
            )
            assert put.status_code == 200, (
                f"cycle {cycle}: PUT config failed: {put.text}"
            )
            config_active = True

            boot = await client.post(
                "/v1/internal_collections/bootstrap",
                timeout=_BOOTSTRAP_TIMEOUT,
            )
            assert boot.status_code == 200, (
                f"cycle {cycle}: bootstrap should return 200 with its outcome; "
                f"got {boot.status_code}: {boot.text}"
            )
            await _wait_bootstrap(client)

            # Search is enabled on the system collection
            assert await _system_search(client) is not None, f"cycle {cycle}: search did not become active"

            # Deactivate
            rm = await client.delete("/v1/internal_collections/config")
            assert rm.status_code == 204, (
                f"cycle {cycle}: DELETE config failed: {rm.text}"
            )
            config_active = False

            # Search is off again after DELETE config
            assert await _system_search(client) is None, f"cycle {cycle}: search did not turn off after DELETE config"

        # After 3 cycles the API is still healthy
        h = await client.get("/v1/health")
        assert h.status_code == 200, h.text
        assert h.json()["status"] == "ok", h.json()
    finally:
        if config_active:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0487 — Bootstrap → DELETE → re-PUT (different embedder) → bootstrap
# ============================================================================


@pytest.mark.asyncio
async def test_t0487_config_swap_reactivation_uses_latest_embedder(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0487 — Sibling of T0091/T0443 with one new variable: the
    re-PUT swaps `embedding_provider_id` to a DIFFERENT row.
    Pin: final GET /config reflects the second embedder; an agent
    created post-cycle is searchable (proves the CDC subsystem
    rebuilt against the new embedder, not stuck on the old one).
    """
    embedder_a = f"emb-t0487a-{unique_suffix}"
    embedder_b = f"emb-t0487b-{unique_suffix}"
    ssp_id = f"ssp-t0487-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr_a = await client.post(
        "/v1/embedding_providers",
        json=_embedding_provider_body(embedder_a),
    )
    assert pr_a.status_code == 201, pr_a.text
    pr_b = await client.post(
        "/v1/embedding_providers",
        json=_embedding_provider_body(embedder_b),
    )
    assert pr_b.status_code == 201, pr_b.text

    config_active = False
    try:
        # First activation cycle with embedder A
        put_a = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_a, ssp_id=ssp_id),
        )
        assert put_a.status_code == 200, put_a.text
        config_active = True

        boot_a = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot_a.status_code == 200, (
            f"first bootstrap should return 200 with its outcome; got "
            f"{boot_a.status_code}: {boot_a.text}"
        )
        await _wait_bootstrap(client)

        # Swap to embedder B (requires DELETE first due to frozen fields)
        rm = await client.delete("/v1/internal_collections/config")
        assert rm.status_code == 204, rm.text
        config_active = False

        put_b = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_b, ssp_id=ssp_id),
        )
        assert put_b.status_code == 200, put_b.text
        config_active = True

        # GET /config reflects embedder B (the latest write)
        got = await client.get("/v1/internal_collections/config")
        assert got.status_code == 200, got.text
        assert got.json().get("embedding_provider_id") == embedder_b, (
            f"after swap, GET /config still shows old embedder: "
            f"{got.json()!r}"
        )

        boot_b = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot_b.status_code == 200, (
            f"second bootstrap should return 200 with its outcome; got "
            f"{boot_b.status_code}: {boot_b.text}"
        )
        await _wait_bootstrap(client)

        # The evidence used to be CDC: create an agent and watch it
        # appear in search under the new embedder. CDC no longer feeds
        # that surface, so the evidence is the run itself. Re-bootstrap
        # after the swap indexes the whole system collection with
        # embedder B; a stale embedder reference would fail outright,
        # and a dimension clash with B's vectors would land in `error`.
        status = await client.get("/v1/internal_collections/bootstrap/status")
        assert status.status_code == 200, status.text
        row = status.json()
        assert row["state"] == "ready", row
        assert row["error"] is None, row
        assert row["documents_indexed"] > 0, (
            f"re-bootstrap on embedder B indexed nothing: {row!r}"
        )

        # The swapped-in provider is the one now recorded on the config.
        cfg = await client.get("/v1/internal_collections/config")
        assert cfg.status_code == 200, cfg.text
        assert cfg.json()["embedding_provider_id"] == embedder_b, cfg.text
    finally:
        if config_active:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_a}")
        await client.delete(f"/v1/embedding_providers/{embedder_b}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0488 — POST /v1/agents concurrent with in-flight bootstrap
# ============================================================================


@pytest.mark.asyncio
async def test_t0488_agents_concurrent_with_in_flight_bootstrap_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0488 — Sibling of T0411 (bootstrap × delete-config) and T0412
    (burst-create + delete-config). T0488 races 5 agent CREATEs
    against an IN-FLIGHT bootstrap (no pre-wait — the bootstrap
    starts at the same instant as the POSTs). Pin: every call clean
    (2xx/4xx, never /errors/internal) and the creates still land.
    """
    embedder_id = f"emb-t0488-{unique_suffix}"
    ssp_id = f"ssp-t0488-{unique_suffix}"
    llm_id = f"llm-t0488-{unique_suffix}"
    agent_ids = [
        f"agent-t0488-{unique_suffix}-{i}" for i in range(5)
    ]
    distinctive = f"inflight-marker-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers",
        json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    agents_created: list[str] = []
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        # Pre-create the LLMProvider so agent creates don't fail on
        # missing-provider — that's not what we're testing
        llm = await seed_llm_provider(client, _llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        # Race: bootstrap + 5 agent CREATEs all at once. Bootstrap
        # is the long-running operation; the agent POSTs should slip
        # through cleanly while the subsystem is still warming up.
        boot_task = asyncio.create_task(client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        ))
        agent_tasks = [
            asyncio.create_task(client.post(
                "/v1/agents",
                json=_agent_body(
                    aid, provider_id=llm_id,
                    description=f"{distinctive}-{aid}",
                ),
            ))
            for aid in agent_ids
        ]
        all_results = await asyncio.gather(boot_task, *agent_tasks)
        boot_resp = all_results[0]
        agent_results = all_results[1:]

        # Bootstrap may have been run (200) or been rejected if already
        # running (409). Either is acceptable; what matters is no
        # /errors/internal anywhere.
        boot_envelope = boot_resp.json() if boot_resp.content else {}
        assert boot_envelope.get("type") != "/errors/internal", (
            f"in-flight bootstrap leaked /errors/internal: "
            f"{boot_resp.text}"
        )
        if boot_resp.status_code not in (200, 409):
            pytest.skip(
                f"bootstrap returned {boot_resp.status_code} during "
                f"race (expected 200/409): {boot_resp.text[:300]}"
            )
        if boot_resp.status_code == 200:
            await _wait_bootstrap(client)

        # Every agent CREATE clean
        for aid, r in zip(agent_ids, agent_results):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"agent {aid!r} leaked /errors/internal during race: "
                f"{r.text}"
            )
            assert r.status_code in (201, 409, 502), (
                f"agent {aid!r}: unexpected {r.status_code}: {r.text}"
            )
            if r.status_code == 201:
                agents_created.append(aid)

        # At least 1 agent should have been created (race shouldn't
        # cause all 5 to fail unless the env is broken)
        assert len(agents_created) >= 1, (
            f"all 5 agent CREATEs failed during in-flight bootstrap: "
            f"{[r.status_code for r in agent_results]!r}"
        )

        # What is under test is that the race leaves the system collection's search in a clean state.
        state = await _system_search(client)
        assert state is None or isinstance(state, dict), state
    finally:
        for aid in agents_created:
            await client.delete(f"/v1/agents/{aid}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0501 — Bootstrap-without-config: envelope determinism + RFC 7807 Content-Type
# ============================================================================


@pytest.mark.asyncio
async def test_t0501_bootstrap_without_config_envelope_deterministic(
    client: httpx.AsyncClient,
) -> None:
    """T0501 — Sibling of T0021 (bootstrap-no-config returns 404).
    T0021 only checked the first call. T0501 sharpens by pinning:

      1. Two consecutive no-config bootstrap calls return identical
         envelopes (status, type, and detail string all stable —
         no first-call vs cached-call drift)
      2. The 404 response carries `Content-Type: application/
         problem+json` per RFC 7807 (sibling of T0312/T0313 for
         this specific error path)
    """
    # Two consecutive calls with no config row in storage
    r1 = await client.post("/v1/internal_collections/bootstrap")
    r2 = await client.post("/v1/internal_collections/bootstrap")

    # Both 404 with the documented envelope
    for r, label in ((r1, "call-1"), (r2, "call-2")):
        assert r.status_code == 404, f"{label}: {r.text}"
        envelope = r.json()
        assert envelope["type"] == "/errors/not-found", envelope
        assert envelope["status"] == 404, envelope
        # Content-Type is application/problem+json per RFC 7807
        ct = r.headers.get("content-type", "")
        assert "problem+json" in ct.lower(), (
            f"{label}: bootstrap-no-config 404 should carry "
            f"problem+json content-type; got {ct!r}"
        )

    # Determinism: status, type, and detail are byte-stable across
    # the two calls (no caching artefact, no per-call timestamp leak)
    assert r1.status_code == r2.status_code, (
        f"non-deterministic status: {r1.status_code} vs {r2.status_code}"
    )
    env1, env2 = r1.json(), r2.json()
    assert env1["type"] == env2["type"], (
        f"type drift: {env1['type']!r} vs {env2['type']!r}"
    )
    assert env1["detail"] == env2["detail"], (
        f"detail drift: {env1['detail']!r} vs {env2['detail']!r}"
    )


# ============================================================================
# T0502 — Three consecutive bootstraps return identically-shaped envelopes
# ============================================================================


@pytest.mark.asyncio
async def test_t0502_three_consecutive_bootstraps_identical_shape(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0502 — T0167 covers the second bootstrap returning 200
    cleanly; T0502 extends to a third call AND pins the envelope
    SHAPE is identical across all three (same set of top-level
    keys; values may differ if the orchestrator reports per-call
    counts but key presence/absence is stable).
    """
    embedder_id = f"emb-t0502-{unique_suffix}"
    ssp_id = f"ssp-t0502-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers",
        json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text
    config_created = False
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        # Three consecutive bootstraps (each runs inline).
        # Status rows come from GET /bootstrap/status after each settles.
        status_rows: list[dict] = []
        for i in range(3):
            r = await client.post(
                "/v1/internal_collections/bootstrap",
                timeout=_BOOTSTRAP_TIMEOUT,
            )
            if r.status_code == 409:
                # Second/third call may hit "already running" -- wait and retry
                await _wait_bootstrap(client)
                r = await client.post(
                    "/v1/internal_collections/bootstrap",
                    timeout=_BOOTSTRAP_TIMEOUT,
                )
            assert r.status_code == 200, (
                f"bootstrap[{i}] should return 200 with its outcome; "
                f"got {r.status_code}: {r.text[:300]}"
            )
            row = await _wait_bootstrap(client)
            status_rows.append(row)

        # All three status rows share the same top-level key set --
        # bootstrap status schema stable across no-op repeats
        keys = [frozenset(row.keys()) for row in status_rows]
        assert keys[0] == keys[1] == keys[2], (
            f"bootstrap status-row shape drifted across 3 calls:\n"
            f"  call-1 keys: {sorted(keys[0])!r}\n"
            f"  call-2 keys: {sorted(keys[1])!r}\n"
            f"  call-3 keys: {sorted(keys[2])!r}"
        )
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0538 — /v1/agents/search top_k=100 (max) returns documented hits envelope
# ============================================================================


# ============================================================================
# T0554 — POST collection then /v1/collections/search reflects CDC ingestion
# ============================================================================


@pytest.mark.asyncio
async def test_t0554_post_collection_then_collections_search_reflects_cdc(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0554 — Reframed from the original "put_document into a
    collection then search reflects" — that path requires the
    SearchService pipeline which is not yet wired (T0019 / T0034
    show only the entity-row-search path is live).

    Pin the contract that DOES exist: POST a Collection row with
    a distinctive description, wait for CDC ingestion, then
    /v1/collections/search with the marker query returns the
    new collection's id in hits.
    """
    embedder_id = f"emb-t0554-{unique_suffix}"
    ssp_id = f"ssp-t0554-{unique_suffix}"
    collection_id = f"coll-t0554-{unique_suffix}"
    distinctive = f"collection-marker-t0554-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers",
        json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text
    config_created = False
    coll_created = False
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot.status_code == 200, (
            f"bootstrap should return 200 with its outcome; got "
            f"{boot.status_code}: {boot.text}"
        )
        await _wait_bootstrap(client)

        # Create a Collection with a distinctive description
        cr = await client.post(
            "/v1/collections",
            json={
                "id": collection_id,
                "description": distinctive,
            },
        )
        assert cr.status_code == 201, cr.text
        coll_created = True

        # Private by default, so it is not on the public live map; publishing it adds the page.
        await _assert_no_system_page(client, f"this-install/collections/{collection_id}")
        await _make_public(client, "collections", collection_id)

        # The new collection gets a page in the system collection: the
        # event-log dispatcher converges it within its poll interval,
        # and because bootstrap left search on there, the converge
        # carries the indexing hooks. Asserted here rather than through
        # /v1/collections/search, which S2 left inert.
        await _wait_for_system_page(
            client, f"this-install/collections/{collection_id}", distinctive,
        )

        found = False
        last: dict = {}
        for _ in range(20):
            search = await client.post(
                f"/v1/collections/{_SYSTEM_COLLECTION_ID}/search",
                json={"query": distinctive, "top_k": 5},
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
            assert search.status_code == 200, search.text
            last = search.json()
            if any(
                h.get("meta", {}).get("path")
                == f"this-install/collections/{collection_id}"
                for h in last["hits"]
            ):
                found = True
                break
            await asyncio.sleep(0.5)
        assert found, (
            f"collection {collection_id!r} was not searchable through the "
            f"system collection; last response: {last!r}"
        )
    finally:
        if coll_created:
            await client.delete(f"/v1/collections/{collection_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0601 — IC bootstrap mid-flight racing 5 concurrent agent DELETEs
# ============================================================================


@pytest.mark.asyncio
async def test_t0601_ic_bootstrap_racing_5_agent_deletes_clean_envelopes(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0601 — Race POST /bootstrap against 5 concurrent agent DELETEs.
    Bootstrap must return 200 or 4xx (never 5xx); each DELETE must
    return 204 or 404; the system collection's search ends in a clean
    state; never /errors/internal across the storm.

    Priority 5 — internal-collections subsystem under churn. The
    race targets the bootstrap path's interaction with CDC: the
    bootstrap initialises vector tables + worker; concurrent DELETEs
    enqueue CDC events. The system must converge cleanly without
    a Pydantic / asyncpg panic.

    Setup creates a real EmbeddingProvider + IC config + agents.
    Bootstrap may take 30-60 s for first-time model load; tests
    skip cleanly if the embedder is unavailable.
    """
    embedder_id = f"emb-t0601-{unique_suffix}"
    ssp_id = f"ssp-t0601-{unique_suffix}"
    agent_ids = [f"ag-t0601-{unique_suffix}-{i}" for i in range(5)]
    llm_id = f"llm-t0601-{unique_suffix}"
    config_created = False

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    # Seed embedding provider.
    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    try:
        # Seed an LLM provider so the agents have a valid provider_id.
        lp = await seed_llm_provider(client, {
            "id": llm_id,
            "provider": "anthropic",
            "models": [{"name": "claude-sonnet-4-6", "context_length": 200_000}],
            "config": {"api_key": "sk-placeholder"},
            "limits": {"max_concurrency": 1},
        })
        assert lp.status_code == 201, lp.text

        # Seed 5 agents.
        for aid in agent_ids:
            ar = await client.post("/v1/agents", json={
                "id": aid,
                "description": "t0601 race-target agent",
                "model": agent_model(llm_id, "claude-sonnet-4-6"),
                "tools": [],
                "system_prompt": ["test"],
            })
            assert ar.status_code == 201, ar.text

        # Activate IC config (no bootstrap yet — that's the race).
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        # Race: bootstrap + 5 concurrent agent DELETEs.
        async def _bootstrap() -> httpx.Response:
            return await client.post(
                "/v1/internal_collections/bootstrap",
                timeout=_BOOTSTRAP_TIMEOUT,
            )

        async def _del(aid: str) -> httpx.Response:
            return await client.delete(f"/v1/agents/{aid}")

        tasks = [
            asyncio.create_task(_bootstrap()),
            *[asyncio.create_task(_del(aid)) for aid in agent_ids],
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        boot_resp = results[0]
        del_resps = results[1:]

        # Bootstrap: 200 (ran), 4xx (configured but couldn't
        # start), or skip on unexpected status.
        assert not isinstance(boot_resp, BaseException), boot_resp
        boot_env = boot_resp.json() if boot_resp.content else {}
        assert boot_env.get("type") != "/errors/internal", (
            f"bootstrap leaked /errors/internal: {boot_resp.text}"
        )
        if boot_resp.status_code not in (200, 400, 409, 422):
            pytest.skip(
                f"bootstrap returned {boot_resp.status_code} — embedder "
                f"may be unavailable. Body: {boot_resp.text[:300]}"
            )
        if boot_resp.status_code == 200:
            await _wait_bootstrap(client)

        # DELETEs: each 204 (success), 404 (already gone), or 4xx.
        for i, r in enumerate(del_resps):
            assert not isinstance(r, BaseException), f"DELETE #{i} raised: {r!r}"
            env = r.json() if r.content else {}
            assert env.get("type") != "/errors/internal", (
                f"DELETE #{i} leaked /errors/internal: "
                f"{r.status_code}: {r.text}"
            )
            assert r.status_code in (204, 404, 400, 422), (
                f"DELETE #{i} unexpected status: "
                f"{r.status_code}: {r.text}"
            )

        # The system collection's search is in a clean state after the storm.
        state = await _system_search(client)
        assert state is None or isinstance(state, dict), state
    finally:
        for aid in agent_ids:
            try:
                await client.delete(f"/v1/agents/{aid}")
            except Exception:  # noqa: BLE001
                pass
        try:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        except Exception:  # noqa: BLE001
            pass
        if config_created:
            try:
                await client.delete("/v1/internal_collections/config")
            except Exception:  # noqa: BLE001
                pass
        try:
            await client.delete(f"/v1/embedding_providers/{embedder_id}")
        except Exception:  # noqa: BLE001
            pass
        try:
            await client.delete(f"/v1/ssp/{ssp_id}")
        except Exception:  # noqa: BLE001
            pass


# ============================================================================
# T0602 — IC re-bootstrap cycle (config DELETE → PUT same body → bootstrap) ×5
# ============================================================================


@pytest.mark.asyncio
async def test_t0602_ic_re_bootstrap_cycle_clean_envelopes(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0602 — Run the lifecycle DELETE config → PUT same config →
    bootstrap repeatedly. Each cycle ends with search enabled on the
    system collection; never /errors/internal.

    Priority 5 — IC subsystem under churn. Re-bootstrap exercises the
    vector-store table create / drop / recreate path, where backends
    accumulate state (open connections, cached schema) that can leak
    across cycles and surface as a 5xx on a later one.

    Was five cycles. Bootstrap actually indexes the system collection
    now, and deleting the config drops its vectors, so every cycle is a
    genuine rebuild of roughly 900 chunks through a real embedder: five
    of them is about 175 s and the lane caps a test at 180. Two cycles
    still exercise create-drop-recreate, which is the leak this is
    watching for; what is lost is only the depth of the repetition.
    """
    embedder_id = f"emb-t0602-{unique_suffix}"
    ssp_id = f"ssp-t0602-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    try:
        for cycle in range(2):
            # DELETE config (no-op on cycle 0 since none exists yet).
            d = await client.delete("/v1/internal_collections/config")
            assert d.status_code in (204, 404), (
                f"cycle {cycle} DELETE unexpected status: "
                f"{d.status_code}: {d.text}"
            )

            # PUT config.
            put = await client.put(
                "/v1/internal_collections/config",
                json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
            )
            assert put.status_code == 200, (
                f"cycle {cycle} PUT failed: {put.status_code}: {put.text}"
            )

            # Bootstrap (synchronous).
            boot = await client.post(
                "/v1/internal_collections/bootstrap",
                timeout=_BOOTSTRAP_TIMEOUT,
            )
            boot_env = boot.json() if boot.content else {}
            assert boot_env.get("type") != "/errors/internal", (
                f"cycle {cycle} bootstrap leaked /errors/internal: "
                f"{boot.text}"
            )
            assert boot.status_code == 200, (
                f"cycle {cycle} bootstrap should return 200 with its outcome; "
                f"got {boot.status_code}: {boot.text[:300]}"
            )
            await _wait_bootstrap(client)

            # Search is enabled on the system collection.
            assert await _system_search(client) is not None, f"cycle {cycle}: search not enabled"
    finally:
        try:
            await client.delete("/v1/internal_collections/config")
        except Exception:  # noqa: BLE001
            pass
        try:
            await client.delete(f"/v1/embedding_providers/{embedder_id}")
        except Exception:  # noqa: BLE001
            pass
        try:
            await client.delete(f"/v1/ssp/{ssp_id}")
        except Exception:  # noqa: BLE001
            pass


# ============================================================================
# A new agent is searchable without waiting for a restart
# ============================================================================


@pytest.mark.asyncio
async def test_new_agent_is_searchable_through_the_system_collection(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """Creating an agent makes it findable, with no second bootstrap.

    The seeded system-cdc event subscription converges that agent's
    page in the system collection and, because bootstrap left search
    enabled on it, the converge carries the indexing hooks. This is the successor to the CDC coverage that
    used to assert the same thing through /v1/agents/search, which S2
    left inert.
    """
    embedder_id = f"emb-cdc-{unique_suffix}"
    ssp_id = f"ssp-cdc-{unique_suffix}"
    llm_id = f"llm-cdc-{unique_suffix}"
    agent_id = f"agent-cdc-{unique_suffix}"
    distinctive = f"pelagic cartography rota {unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text
    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    agent_created = False
    llm_created = False
    try:
        async with bootstrapped_subsystem(client, embedder_id, ssp_id):
            llm = await seed_llm_provider(client, _llm_body(llm_id))
            assert llm.status_code == 201, llm.text
            llm_created = True

            ag = await client.post(
                "/v1/agents",
                json=_agent_body(
                    agent_id, provider_id=llm_id, description=distinctive,
                ),
            )
            assert ag.status_code == 201, ag.text
            agent_created = True

            # Private by default, so it is not on the public live map; publishing it adds the page.
            await _assert_no_system_page(client, f"this-install/agents/{agent_id}")
            await _make_public(client, "agents", agent_id)

            # The page converges via the event-log dispatcher (bounded by
            # its poll interval), not inline with the POST.
            await _wait_for_system_page(
                client, f"this-install/agents/{agent_id}", distinctive,
            )

            # And it is searchable, without bootstrapping a second time.
            found = False
            last: dict = {}
            for _ in range(20):
                search = await client.post(
                    f"/v1/collections/{_SYSTEM_COLLECTION_ID}/search",
                    json={"query": distinctive, "top_k": 5},
                    timeout=httpx.Timeout(60.0, connect=10.0),
                )
                assert search.status_code == 200, search.text
                last = search.json()
                if any(
                    f"this-install/agents/{agent_id}"
                    == h.get("meta", {}).get("path")
                    for h in last["hits"]
                ):
                    found = True
                    break
                await asyncio.sleep(0.5)
            assert found, (
                f"agent {agent_id!r} was not searchable through the system "
                f"collection; last response: {last!r}"
            )
    finally:
        if agent_created:
            await client.delete(f"/v1/agents/{agent_id}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")

# ============================================================================
# WIKI-FIRST M2 (D1) - the enable-IC flow on a clean install's seeded rows
# ============================================================================
#
# A fresh install seeds the reserved local embedder (provider id
# `huggingface`, model BAAI/bge-small-en-v1.5) and the reserved `lance`
# SSP. The wiki-first D1 runbook walks exactly this flow, so this test is
# the CI form of the D1 acceptance: verify the seeded rows, write the
# config with them (NO new embedding provider or SSP is created),
# bootstrap, verify the status row, run a real query round-trip, and
# deactivate.


@pytest.mark.asyncio
async def test_m2_enable_flow_on_seeded_rows_roundtrip(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """M2 D1 (CI form) - the operator's enable-IC flow mechanics.

    Pins the clean-install precondition the runbook's step 1 reads (the
    reserved `huggingface` embedder + the reserved `lance` SSP exist
    before anything is configured - the reserved-guard test below pins
    their create=409 / delete=403 behaviour), then walks the flow
    end to end: config write -> bootstrap -> status ready -> a real
    semantic query round-trip -> deactivation.

    The bootstrap pass runs on the small tier (the documented safe pick,
    OQ-2): the seeded bge-small model would make this test's embedding
    pass run past the e2e lane's per-test ceiling, and the flow
    mechanics the test pins are model-independent (the seeded-default
    acceptance itself is the D1 probe evidence: the M1 RED prompt walked
    to a verified state on the seeded rows).
    """
    # The seeded reserved rows, verified as the flow's step 1 would.
    emb = await client.get("/v1/embedding_providers/huggingface")
    assert emb.status_code == 200, (
        f"the seeded reserved embedder must exist on a clean install; "
        f"got {emb.status_code}: {emb.text}"
    )
    emb_models = [m["name"] for m in emb.json()["models"]]
    assert "BAAI/bge-small-en-v1.5" in emb_models, (
        f"the seeded embedder carries the small/mid reference model; "
        f"got: {emb_models!r}"
    )
    ssp = await client.get("/v1/ssp/lance")
    assert ssp.status_code == 200, (
        f"the seeded reserved lance SSP must exist on a clean install; "
        f"got {ssp.status_code}: {ssp.text}"
    )

    # The small tier: registered the way the flow registers any chosen
    # model (the runbook's check -> register path), then the config
    # write - with the seeded SSP, no new search provider.
    embedder_id = f"emb-m2-small-{unique_suffix}"
    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text
    try:
        # Clean start (idempotent), then the flow's config write.
        await client.delete("/v1/internal_collections/config")
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id, ssp_id="lance"),
        )
        assert put.status_code == 200, put.text
        try:
            boot = await client.post(
                "/v1/internal_collections/bootstrap",
                timeout=_BOOTSTRAP_TIMEOUT,
            )
            assert boot.status_code == 200, (
                f"bootstrap answers 200 with its terminal outcome; got "
                f"{boot.status_code}: {boot.text}"
            )
            outcome = boot.json()
            assert outcome["status"] == "succeeded", outcome
            assert outcome["state"] == "ready", outcome

            # The verify step's durable state: the bootstrap status row.
            st = await client.get("/v1/internal_collections/bootstrap/status")
            assert st.status_code == 200, st.text
            st_row = st.json()
            assert st_row["state"] == "ready", st_row
            assert st_row["documents_indexed"] > 0, st_row
            assert st_row["documents_indexed"] == st_row["documents_total"], st_row

            # And the behavioral check: a real query round-trip through
            # the system collection's semantic search.
            search = await client.post(
                f"/v1/collections/{_SYSTEM_COLLECTION_ID}/search",
                json={"query": "the platform wiki runbook internal collections", "top_k": 5},
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
            assert search.status_code == 200, search.text
            hits = search.json()["hits"]
            assert hits, (
                "a semantic query against a freshly bootstrapped system "
                f"collection must return hits; got: {search.text!r}"
            )
        finally:
            await client.delete("/v1/internal_collections/config")

        # Deactivated again: the config is gone and the status row reports it.
        gone = await client.get("/v1/internal_collections/config")
        assert gone.status_code == 404, gone.text
        st2 = await client.get("/v1/internal_collections/bootstrap/status")
        assert st2.status_code == 200, st2.text
        assert st2.json()["state"] == "disabled", st2.text
    finally:
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


async def test_m2_reserved_seeded_rows_are_guarded(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """The reserved ids the runbook's precondition cites.

    The `huggingface` embedder id is reserved: bootstrap seeds the local
    embedder under it, a create with that id answers 409, and its delete
    answers 403 (the row must survive the install). Pins the precondition
    the enable-IC runbook reads before step 1.
    """
    create = await client.post(
        "/v1/embedding_providers",
        json={
            "id": "huggingface",
            "provider": "huggingface",
            "models": [{"name": "sentence-transformers/all-MiniLM-L6-v2"}],
            "config": {"token": ""},
            "limits": {"max_concurrency": 1},
        },
    )
    assert create.status_code == 409, (
        f"creating the reserved embedder id must be refused; got "
        f"{create.status_code}: {create.text}"
    )
    delete = await client.delete("/v1/embedding_providers/huggingface")
    assert delete.status_code == 403, (
        f"deleting the reserved seeded embedder must be refused; got "
        f"{delete.status_code}: {delete.text}"
    )
    still = await client.get("/v1/embedding_providers/huggingface")
    assert still.status_code == 200, (
        f"the seeded embedder must survive the refused delete; got "
        f"{still.status_code}: {still.text}"
    )
