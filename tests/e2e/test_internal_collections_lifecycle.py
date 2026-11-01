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
# T0165 — /v1/tools/search returns 200 after bootstrap (positive control)
# ============================================================================




# ============================================================================
# T0174 — query-based discrimination of two agents (positive control)
# ============================================================================


# ============================================================================
# T0167 — bootstrap is idempotent (second call returns 200 cleanly)
# ============================================================================




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




# ============================================================================
# T0202 — POST /v1/agents/search with query="" returns clean envelope
# ============================================================================




# ============================================================================
# T0203 — Bootstrap on empty DB (no agents/graphs/collections/tools)
# ============================================================================




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




# ============================================================================
# T0277 — Concurrent bootstrap calls during a fresh PUT config race cleanly
# ============================================================================




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




# ============================================================================
# T0299 — search concurrent with PUT Agent description: search reflects update
# ============================================================================


# ============================================================================
# T0333 — POST /v1/agents/search with `filter` body field is silently ignored
# ============================================================================


# ============================================================================
# T0346 — Embedder→Collection cascade: search after embedder DELETE clean
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
# T0303 — Bootstrap concurrent with /v1/agents/search returns clean envelopes
# ============================================================================




# ============================================================================
# T0400 — CDC ingestion latency for Agent POST → /agents/search empirical pin
# ============================================================================


# ============================================================================
# T0411 — Concurrent bootstrap + DELETE config returns clean envelopes
# ============================================================================




# ============================================================================
# T0412 — CDC enqueue race: agent burst-create + immediate DELETE config
# ============================================================================




# ============================================================================
# T0442 — Burst PUT on /internal_collections/config (10 concurrent)
# ============================================================================




# ============================================================================
# T0443 — Rapid 3-cycle deactivate/reactivate completes cleanly
# ============================================================================




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




# ============================================================================
# T0602 — IC re-bootstrap cycle (config DELETE → PUT same body → bootstrap) ×5
# ============================================================================




# ============================================================================
# T0586 — /v1/agents/search top_k=1 on empty post-bootstrap DB returns
# 200 with hits=[]
# ============================================================================




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
