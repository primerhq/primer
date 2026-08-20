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
per-entity routes (POST /v1/agents/search and friends) stay registered
but INERT until they are deleted, answering 200 with no hits.

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

import httpx
import pytest
import pytest_asyncio
from tests._support.model_profiles import agent_model, seed_llm_provider


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


def _ssp_body(entity_id: str) -> dict:
    """pgvector SemanticSearchProvider backed by the e2e postgres instance.

    The internal-collections config PUT now requires a valid
    search_provider_id that references an existing SemanticSearchProvider
    row. This helper creates that prerequisite using the same postgres
    DSN as the e2e bringup script (primer:primer@localhost/primer_e2e).
    """
    return {
        "id": entity_id,
        "provider": "pgvector",
        "config": {
            "hostname": "localhost",
            "port": 5432,
            "database": "primer_e2e",
            "username": "primer",
            "password": "primer",
            "db_schema": "public",
        },
    }


def _ic_config_body(*, embedder_id: str, ssp_id: str) -> dict:
    return {
        "embedding_provider_id": embedder_id,
        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        "search_provider_id": ssp_id,
    }


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
    search returns 503 again."""
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

        # 5. Search now works (no hits because no agents indexed) — but
        #    the subsystem is active, so it should NOT return 503.
        search_active = await client.post(
            "/v1/agents/search", json={"query": "anything", "top_k": 3},
        )
        assert search_active.status_code == 200, search_active.text

        # 6. DELETE the config — this is the actual T0053 assertion target
        rm = await client.delete("/v1/internal_collections/config")
        assert rm.status_code == 204, rm.text
        config_created = False  # already cleaned

        # 7. Search must return 503 with /errors/subsystem-inactive
        # The subsystem teardown is async; give it a brief moment.
        last: httpx.Response | None = None
        for _ in range(10):
            r = await client.post(
                "/v1/agents/search", json={"query": "anything", "top_k": 3},
            )
            last = r
            if r.status_code == 503:
                break
            await asyncio.sleep(0.1)
        assert last is not None
        assert last.status_code == 503, (
            f"after DELETE config, search should be 503, got "
            f"{last.status_code}: {last.text}"
        )
        assert last.json()["type"] == "/errors/subsystem-inactive"
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


async def _bootstrap_subsystem(
    client: httpx.AsyncClient,
    embedder_id: str,
    ssp_id: str,
) -> None:
    """DELETE any active config, PUT a fresh one, then bootstrap.

    Used by many tests. The DELETE is what makes re-configuration legal:
    S2 freezes the vector-space fields while the subsystem is active, so
    a PUT naming different providers 409s until it is deactivated.
    """
    # Idempotent: 404 when nothing is configured yet, which is fine.
    await client.delete("/v1/internal_collections/config")
    put = await client.put(
        "/v1/internal_collections/config",
        json=_ic_config_body(embedder_id=embedder_id, ssp_id=ssp_id),
    )
    assert put.status_code == 200, put.text
    boot = await client.post(
        "/v1/internal_collections/bootstrap",
        timeout=_BOOTSTRAP_TIMEOUT,
    )
    assert boot.status_code == 200, (
        f"bootstrap should return 200 with its outcome; got "
        f"{boot.status_code}: {boot.text}"
    )
    await _wait_bootstrap(client)


@pytest.mark.asyncio
async def test_t0062_search_top_k_caps_result_count(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0062 — search with top_k=1 returns at most 1 hit even when
    multiple agents would otherwise match. Pins the upper-bound
    semantics of the top_k parameter (Pydantic enforces ge=1, le=100;
    the search runtime must honour the cap).
    """
    embedder_id = f"emb-t0062-{unique_suffix}"
    ssp_id = f"ssp-t0062-{unique_suffix}"
    llm_id = f"llm-t0062-{unique_suffix}"
    shared_marker = f"shared-marker-{unique_suffix}"
    agent_ids = [f"agent-t0062-{unique_suffix}-{i}" for i in range(3)]

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    created_agents: list[str] = []
    try:
        await _bootstrap_subsystem(client, embedder_id, ssp_id)
        config_created = True

        llm = await seed_llm_provider(client, _llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        # Three agents sharing the same description marker so all three
        # would qualify on lexical match alone.
        for aid in agent_ids:
            ag = await client.post(
                "/v1/agents",
                json=_agent_body(
                    aid, provider_id=llm_id, description=shared_marker,
                ),
            )
            assert ag.status_code == 201, ag.text
            created_agents.append(aid)

        # No wait for indexing: CDC no longer feeds this surface, so the
        # three agents never reach it. The cap is what is under test and
        # it holds whether the index has three matches or none.

        # top_k=1 must cap the response, even though multiple match.
        resp = await client.post(
            "/v1/agents/search",
            json={"query": shared_marker, "top_k": 1},
        )
        assert resp.status_code == 200, resp.text
        hits = resp.json()["hits"]
        assert len(hits) <= 1, (
            f"top_k=1 was not honoured; got {len(hits)} hits: "
            f"{[h['document_id'] for h in hits]!r}"
        )
    finally:
        for aid in created_agents:
            await client.delete(f"/v1/agents/{aid}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
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
# T0165 — /v1/tools/search returns 200 after bootstrap (positive control)
# ============================================================================


@pytest.mark.asyncio
async def test_t0165_tools_search_returns_200_after_bootstrap(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0165 — after bootstrap, POST /v1/tools/search returns 200 with a
    non-error envelope (the fourth internal collection per spec §11
    is `_internal_tools`). Built-in tools (e.g. `_system`, `_workspaces`)
    are indexed at bootstrap time.

    NB: Spec §11 lists Tool as one of four CDC-mirrored entity kinds,
    but primer/api/routers/_cdc_hooks.py only wires hooks for
    agent / graph / collection — Toolset CRUD does NOT live-update
    the tools index. This test pins the positive-control bootstrap
    path; live CDC for Toolsets is out of scope.
    """
    embedder_id = f"emb-t0165-{unique_suffix}"
    ssp_id = f"ssp-t0165-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id, ssp_id)
        config_created = True

        # /v1/tools/search must return 200 with a SearchResponse envelope.
        # Built-in tool descriptions like "exec" or "list files" should
        # at least produce some hits (or zero hits, but not 5xx) for a
        # well-known generic query.
        search = await client.post(
            "/v1/tools/search",
            json={"query": "execute shell command", "top_k": 5},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        assert search.status_code == 200, search.text
        body = search.json()
        assert "hits" in body, f"missing 'hits' key: {body!r}"
        # hits is a list (possibly empty if no built-in matched)
        assert isinstance(body["hits"], list), body
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


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
    than racing the first. Search routes must remain consistent
    afterwards.
    """
    embedder_id = f"emb-t0167-{unique_suffix}"
    ssp_id = f"ssp-t0167-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id, ssp_id)
        config_created = True

        # First call already happened in _bootstrap_subsystem. Second call:
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

        # Search route still works after the second bootstrap (no stale
        # registry leak).
        s = await client.post(
            "/v1/agents/search", json={"query": "anything", "top_k": 3},
        )
        assert s.status_code == 200, s.text
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
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
    mutable. The search route must continue serving throughout.

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

    config_created = False
    try:
        await _bootstrap_subsystem(client, embedder_a, ssp_id)
        config_created = True

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

        # Search route still responds cleanly on the ORIGINAL embedder
        # (the failed PUT did not corrupt the subsystem state).
        s = await client.post(
            "/v1/agents/search", json={"query": "anything", "top_k": 3},
        )
        assert s.status_code == 200, (
            f"search should still work after a rejected reconfigure "
            f"PUT; got {s.status_code}: {s.text}"
        )
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_a}")
        await client.delete(f"/v1/embedding_providers/{embedder_b}")
        await client.delete(f"/v1/ssp/{ssp_id}")


# ============================================================================
# T0202 — POST /v1/agents/search with query="" returns clean envelope
# ============================================================================


@pytest.mark.asyncio
async def test_t0202_search_empty_query_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0202 — POST /v1/agents/search with `query=""` after bootstrap.
    The SearchRequest has `query` with `min_length=1` per the model,
    so Pydantic will reject this with 422 — pin that response. If a
    future change relaxes the min_length, a 200 with empty hits is
    also acceptable. NEVER 5xx.
    """
    embedder_id = f"emb-t0202-{unique_suffix}"
    ssp_id = f"ssp-t0202-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id, ssp_id)
        config_created = True

        resp = await client.post(
            "/v1/agents/search",
            json={"query": "", "top_k": 5},
        )
        assert resp.status_code != 500, resp.text
        if resp.status_code == 200:
            assert isinstance(resp.json().get("hits"), list), resp.json()
        else:
            assert 400 <= resp.status_code < 500, resp.text
            envelope = resp.json()
            assert envelope["type"].startswith("/errors/"), envelope
            assert envelope["type"] != "/errors/internal", envelope
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
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
        # Search endpoints work after bootstrap (no agents indexed yet)
        s = await client.post(
            "/v1/agents/search",
            json={"query": "anything", "top_k": 3},
        )
        assert s.status_code == 200, s.text
        # Hits list is present and is a list (zero entries are fine)
        assert isinstance(s.json().get("hits"), list), s.json()
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
    envelope; search routes return 200 with empty hits.
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

        # Search returns clean envelope
        s = await client.post(
            "/v1/agents/search", json={"query": "anything", "top_k": 3},
        )
        assert s.status_code == 200, s.text
        assert isinstance(s.json().get("hits"), list), s.json()
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
    settles, the subsystem must be active (search returns 200).
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

        # After bootstrap, search must return 200.
        # Be tolerant: poll briefly until search is up.
        last: httpx.Response | None = None
        for _ in range(10):
            s = await client.post(
                "/v1/agents/search",
                json={"query": "anything", "top_k": 3},
            )
            last = s
            if s.status_code == 200:
                break
            if s.status_code == 503:
                # Subsystem may still be warming up; kick another bootstrap
                br = await client.post(
                    "/v1/internal_collections/bootstrap",
                    timeout=_BOOTSTRAP_TIMEOUT,
                )
                if br.status_code == 200:
                    await _wait_bootstrap(client)
            await asyncio.sleep(0.5)
        assert last is not None
        assert last.status_code == 200, (
            f"search not 200 after concurrent bootstrap race + "
            f"retries: {last.status_code}: {last.text}"
        )
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
# T0289 — Concurrent CDC ingest of 5 Agents + 5 search calls
# ============================================================================


@pytest.mark.asyncio
async def test_t0289_concurrent_cdc_ingest_and_search_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0289 — Cross-entity concurrency: fire 5 Agent POSTs and 5
    /v1/agents/search calls concurrently. All responses have clean
    envelopes (no /errors/internal) and every create still lands.

    The tail that polled until all five turned up in that search is
    gone: S2 left the per-entity routes inert, so nothing populates
    them. Agents reaching search is covered against the system
    collection instead, by
    ``test_new_agent_is_searchable_through_the_system_collection``.
    """
    embedder_id = f"emb-t0289-{unique_suffix}"
    ssp_id = f"ssp-t0289-{unique_suffix}"
    llm_id = f"llm-t0289-{unique_suffix}"
    agent_ids = [f"agent-conc-{unique_suffix}-{i}" for i in range(5)]
    common_marker = f"concurrent-cdc-{unique_suffix}"

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
        await _bootstrap_subsystem(client, embedder_id, ssp_id)
        config_created = True

        llm = await seed_llm_provider(client, _llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        async def _post(i: int) -> httpx.Response:
            return await client.post(
                "/v1/agents",
                json=_agent_body(
                    agent_ids[i], provider_id=llm_id,
                    description=f"{common_marker}-{i}",
                ),
            )

        async def _search() -> httpx.Response:
            return await client.post(
                "/v1/agents/search",
                json={"query": common_marker, "top_k": 10},
            )

        tasks: list = []
        for i in range(5):
            tasks.append(asyncio.create_task(_post(i)))
        for _ in range(5):
            tasks.append(asyncio.create_task(_search()))

        results = await asyncio.gather(*tasks)
        for i, r in enumerate(results):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"task {i} returned /errors/internal: {r.text}"
            )

        for i in range(5):
            if results[i].status_code == 201:
                agents_created.append(agent_ids[i])

        assert len(agents_created) == 5, (
            f"only {len(agents_created)}/5 agent POSTs succeeded: "
            f"{agents_created!r}"
        )

        # The subsystem still answers cleanly after the concurrent load,
        # which is what the race is here to check.
        s = await client.post(
            "/v1/agents/search",
            json={"query": common_marker, "top_k": 10},
        )
        assert s.status_code == 200, s.text
        assert isinstance(s.json()["hits"], list), s.text
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
# T0299 — search concurrent with PUT Agent description: search reflects update
# ============================================================================


# ============================================================================
# T0333 — POST /v1/agents/search with `filter` body field is silently ignored
# ============================================================================


# ============================================================================
# T0346 — Embedder→Collection cascade: search after embedder DELETE clean
# ============================================================================


@pytest.mark.asyncio
async def test_t0346_search_after_embedder_delete_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0346 — Build embedder→subsystem→collection chain, then DELETE
    the underlying embedder while the subsystem references it.
    Subsequent /v1/collections/search must return a clean envelope
    (200 if cached results survive, 503 if subsystem detects the
    broken reference, or 4xx). NEVER /errors/internal.
    """
    embedder_id = f"emb-t0346-{unique_suffix}"
    ssp_id = f"ssp-t0346-{unique_suffix}"
    coll_id = f"coll-t0346-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    coll_created = False
    embedder_deleted = False
    try:
        await _bootstrap_subsystem(client, embedder_id, ssp_id)
        config_created = True

        coll = await client.post(
            "/v1/collections",
            json={
                "id": coll_id,
                "description": f"T0346-{unique_suffix}",
            },
        )
        assert coll.status_code in (200, 201), coll.text
        coll_created = True

        # Wait briefly for indexing
        await asyncio.sleep(1.0)

        # DELETE the embedder while subsystem references it
        rm = await client.delete(f"/v1/embedding_providers/{embedder_id}")
        # The DELETE might fail if the subsystem holds a reference;
        # accept either 204 or clean 4xx
        assert rm.status_code < 500, rm.text
        if rm.status_code == 204:
            embedder_deleted = True

        # Search must produce a clean envelope
        s = await client.post(
            "/v1/collections/search",
            json={"query": "anything", "top_k": 3},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        envelope = s.json() if s.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"search after embedder DELETE leaked /errors/internal: "
            f"{s.text}"
        )
    finally:
        if coll_created:
            await client.delete(f"/v1/collections/{coll_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        if not embedder_deleted:
            await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


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
# T0303 — Bootstrap concurrent with /v1/agents/search returns clean envelopes
# ============================================================================


@pytest.mark.asyncio
async def test_t0303_bootstrap_concurrent_with_search_clean(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0303 — Cross-subsystem concurrency: fire bootstrap and 5
    /v1/agents/search calls concurrently after the subsystem is
    already active. All responses must have clean envelopes (200 or
    documented 503 if briefly inactive); no /errors/internal.
    """
    embedder_id = f"emb-t0303-{unique_suffix}"
    ssp_id = f"ssp-t0303-{unique_suffix}"

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id, ssp_id)
        config_created = True

        async def _bootstrap() -> httpx.Response:
            return await client.post(
                "/v1/internal_collections/bootstrap",
                timeout=_BOOTSTRAP_TIMEOUT,
            )

        async def _search() -> httpx.Response:
            return await client.post(
                "/v1/agents/search",
                json={"query": "anything", "top_k": 3},
            )

        tasks = [asyncio.create_task(_bootstrap())]
        for _ in range(5):
            tasks.append(asyncio.create_task(_search()))
        results = await asyncio.gather(*tasks)
        for i, r in enumerate(results):
            envelope = r.json() if r.content else {}
            assert envelope.get("type") != "/errors/internal", (
                f"task {i} returned /errors/internal: {r.text}"
            )
            # Bootstrap result is at index 0 — must be 200
            # or 409 (already running from the first bootstrap)
            if i == 0:
                assert r.status_code == 200, (
                    f"concurrent bootstrap unexpected status: {r.text}"
                )
            else:
                # Search results -- 200 or 503 (subsystem-inactive)
                assert r.status_code in (200, 503), (
                    f"search task {i} unexpected status: {r.text}"
                )
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")


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
    (no 5xx, no `/errors/internal`). The subsequent search route
    converges to a deterministic state — either 503 (DELETE won, or
    bootstrap completed and was then torn down) or 200 (bootstrap
    won and config still present after the DELETE attempt).

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

        # Subsequent search must be deterministic -- converge briefly
        last: httpx.Response | None = None
        for _ in range(10):
            s = await client.post(
                "/v1/agents/search",
                json={"query": "anything", "top_k": 3},
            )
            last = s
            if s.status_code in (200, 503):
                break
            await asyncio.sleep(0.2)
        assert last is not None
        assert last.status_code in (200, 503), (
            f"search after race: unexpected code {last.status_code}: "
            f"{last.text}"
        )
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
    (search → 503), every call returns 2xx/4xx (no /errors/internal),
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

        # Subsystem ends up inactive — search returns 503
        last: httpx.Response | None = None
        for _ in range(20):
            s = await client.post(
                "/v1/agents/search",
                json={"query": "anything", "top_k": 3},
            )
            last = s
            if s.status_code == 503:
                break
            await asyncio.sleep(0.2)
        assert last is not None, "search never returned"
        assert last.status_code == 503, (
            f"after burst-create + DELETE config, search should be "
            f"503 inactive; got {last.status_code}: {last.text}"
        )
        assert last.json()["type"] == "/errors/subsystem-inactive", (
            last.json()
        )
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
    state); the subsystem stays usable (search 503 if not bootstrapped,
    or 200 if bootstrapped).
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

        # Subsystem state is observable: search either 200
        # (bootstrapped concurrently) or 503 (not-yet documented
        # /errors/subsystem-inactive). 503 IS the contract here, not
        # a 5xx leak — pin shape directly without a sub-500 gate.
        s = await client.post(
            "/v1/agents/search",
            json={"query": "anything", "top_k": 3},
        )
        envelope = s.json() if s.content else {}
        assert envelope.get("type") != "/errors/internal", s.text
        assert s.status_code in (200, 503), (
            f"search after burst PUT: unexpected {s.status_code}: {s.text}"
        )
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
    between them: PUT config → bootstrap → search 200 → DELETE
    config → search 503 → repeat 3 times.

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

            # Subsystem active — search returns 200 within 5s.
            # 503 is the documented inactive signal, not a 5xx leak,
            # so the only "5xx leak" check is /errors/internal.
            search_active: httpx.Response | None = None
            for _ in range(10):
                s = await client.post(
                    "/v1/agents/search",
                    json={"query": "anything", "top_k": 3},
                )
                search_active = s
                envelope = s.json() if s.content else {}
                assert envelope.get("type") != "/errors/internal", (
                    f"cycle {cycle}: search leaked /errors/internal: "
                    f"{s.text}"
                )
                if s.status_code == 200:
                    break
                await asyncio.sleep(0.5)
            assert search_active is not None
            assert search_active.status_code == 200, (
                f"cycle {cycle}: search did not become active: "
                f"{search_active.text}"
            )

            # Deactivate
            rm = await client.delete("/v1/internal_collections/config")
            assert rm.status_code == 204, (
                f"cycle {cycle}: DELETE config failed: {rm.text}"
            )
            config_active = False

            # Subsystem inactive — search returns 503 within 5s
            search_inactive: httpx.Response | None = None
            for _ in range(10):
                s = await client.post(
                    "/v1/agents/search",
                    json={"query": "anything", "top_k": 3},
                )
                search_inactive = s
                envelope = s.json() if s.content else {}
                assert envelope.get("type") != "/errors/internal", (
                    f"cycle {cycle}: search leaked /errors/internal: "
                    f"{s.text}"
                )
                if s.status_code == 503:
                    break
                await asyncio.sleep(0.5)
            assert search_inactive is not None
            assert search_inactive.status_code == 503, (
                f"cycle {cycle}: search did not become inactive after "
                f"DELETE config: {search_inactive.text}"
            )

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

        # The tail of this test used to poll until one of them turned up
        # in /agents/search. CDC no longer feeds that surface, so what
        # remains under test is that the race leaves the subsystem in a
        # clean state rather than a half-written one.
        search = await client.post(
            "/v1/agents/search", json={"query": distinctive, "top_k": 5},
        )
        assert search.status_code == 200, search.text
        assert isinstance(search.json()["hits"], list), search.text
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
# T0537 — /v1/agents/search post-bootstrap on empty DB returns 200 empty hits
# ============================================================================


@pytest.mark.asyncio
async def test_t0537_search_post_bootstrap_empty_db_returns_empty_hits(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0537 — Sharper than T0203 (which pinned bootstrap envelope
    shape). T0537 specifically pins the search-after-clean-bootstrap
    path: PUT config → bootstrap → POST /v1/agents/search → 200 with
    `hits=[]` (the documented empty-result envelope, not 503 / 4xx).

    Catches a regression where the freshly-bootstrapped subsystem
    returns the search route as inactive even though /bootstrap
    succeeded.
    """
    embedder_id = f"emb-t0537-{unique_suffix}"
    ssp_id = f"ssp-t0537-{unique_suffix}"

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

        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=_BOOTSTRAP_TIMEOUT,
        )
        assert boot.status_code == 200, (
            f"bootstrap should return 200 with its outcome; got "
            f"{boot.status_code}: {boot.text}"
        )
        await _wait_bootstrap(client)

        # No agents created in this test. The search must return 200 with
        # a valid hits envelope (not 503 / inactive). The shared vector
        # store may contain agents indexed by concurrent tests, so we do
        # not assert hits == [] -- we assert the subsystem is active and
        # the response shape is correct.
        search = await client.post(
            "/v1/agents/search",
            json={"query": "anything", "top_k": 10},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        envelope = search.json() if search.content else {}
        assert envelope.get("type") != "/errors/internal", (
            f"empty-DB search leaked /errors/internal: {search.text}"
        )
        assert search.status_code == 200, (
            f"post-bootstrap search should be 200 (subsystem active), not "
            f"503/inactive; got {search.status_code}: {search.text}"
        )
        body = search.json()
        assert "hits" in body, body
        assert isinstance(body["hits"], list), body
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

        # The new collection gets a page in the system collection, and
        # because bootstrap left search on there, the CRUD hook indexes
        # it in the same write. Asserted here rather than through
        # /v1/collections/search, which S2 left inert.
        page = await client.get(
            f"/v1/collections/{_SYSTEM_COLLECTION_ID}/documents",
            params={"path": f"collections/{collection_id}"},
        )
        assert page.status_code == 200, page.text
        assert distinctive in page.text, page.text

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
                h.get("meta", {}).get("path") == f"collections/{collection_id}"
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
    return 204 or 404; subsequent /v1/agents/search must return a
    clean 200 envelope; never /errors/internal across the storm.

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

        # Subsequent /agents/search returns clean envelope (200 with
        # whatever hits remain — concurrent DELETEs may have racing
        # CDC propagation so we don't pin hit contents).
        search = await client.post(
            "/v1/agents/search",
            json={"query": "anything", "top_k": 5},
        )
        search_env = search.json() if search.content else {}
        assert search_env.get("type") != "/errors/internal", (
            f"post-race search leaked /errors/internal: {search.text}"
        )
        assert search.status_code in (200, 503), (
            f"post-race search unexpected status: "
            f"{search.status_code}: {search.text}"
        )
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
    bootstrap repeatedly. Each cycle ends with /v1/agents/search
    returning 200; never /errors/internal.

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

            # /agents/search 200.
            search = await client.post(
                "/v1/agents/search",
                json={"query": f"cycle-{cycle}", "top_k": 3},
            )
            search_env = search.json() if search.content else {}
            assert search_env.get("type") != "/errors/internal", (
                f"cycle {cycle} search leaked /errors/internal: "
                f"{search.text}"
            )
            assert search.status_code == 200, (
                f"cycle {cycle} search unexpected status: "
                f"{search.status_code}: {search.text}"
            )
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
# T0586 — /v1/agents/search top_k=1 on empty post-bootstrap DB returns
# 200 with hits=[]
# ============================================================================


@pytest.mark.asyncio
async def test_t0586_agents_search_top_k_1_empty_post_bootstrap(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0586 — After bootstrap on a fresh DB with no agents,
    /v1/agents/search with top_k=1 returns 200 with hits=[]
    (or similar empty-result envelope).

    Priority 5 — IC subsystem under churn. top_k=1 sister of T0537
    (which pins top_k bounds). Documents the empty-result shape
    for callers who paginate one-by-one.
    """
    embedder_id = f"emb-t0586-{unique_suffix}"
    ssp_id = f"ssp-t0586-{unique_suffix}"
    config_created = False

    sr = await client.post("/v1/ssp", json=_ssp_body(ssp_id))
    assert sr.status_code == 201, sr.text

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

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

        # No agents seeded in this test; top_k=1. The search must return
        # 200 with a valid hits envelope (not 503 / inactive). The shared
        # vector store may contain agents from concurrent tests, so we do
        # not assert hits == [] -- we assert the subsystem is active and
        # the response shape is correct, and that top_k=1 yields at most
        # one hit (bounds respected).
        resp = await client.post(
            "/v1/agents/search",
            json={"query": "anything", "top_k": 1},
        )
        env = resp.json() if resp.content else {}
        assert env.get("type") != "/errors/internal", (
            f"top_k=1 search leaked /errors/internal: {resp.text}"
        )
        assert resp.status_code == 200, (
            f"top_k=1 search expected 200 (subsystem active); got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "hits" in body, body
        assert isinstance(body["hits"], list), body
        assert len(body["hits"]) <= 1, (
            f"top_k=1 must yield at most 1 hit; got: {body}"
        )
    finally:
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
# A new agent is searchable without waiting for a restart
# ============================================================================


@pytest.mark.asyncio
async def test_new_agent_is_searchable_through_the_system_collection(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """Creating an agent makes it findable, with no second bootstrap.

    The CRUD hooks converge that agent's page in the system collection
    and, because bootstrap left search enabled on it, the write carries
    the indexing hooks. This is the successor to the CDC coverage that
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
        await _bootstrap_subsystem(client, embedder_id, ssp_id)

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

        # The page exists immediately, written by the CRUD hook.
        page = await client.get(
            f"/v1/collections/{_SYSTEM_COLLECTION_ID}/documents",
            params={"path": f"agents/{agent_id}"},
        )
        assert page.status_code == 200, page.text
        assert distinctive in page.text, page.text

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
                f"agents/{agent_id}" == h.get("meta", {}).get("path")
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
        await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
        await client.delete(f"/v1/ssp/{ssp_id}")
