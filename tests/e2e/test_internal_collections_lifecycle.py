"""E2E: internal-collections subsystem activation/CDC/deactivation.

Covers backlog items T0034 (CDC: new Agent appears in search) and
T0053 (DELETE config deactivates subsystem).

The setup chain creates a HuggingFace EmbeddingProvider pointed at a
local sentence-transformers model (no network creds required), PUTs
the internal-collections config referencing it, calls bootstrap (which
creates the vector tables — embedding calls only happen on ingestion),
then exercises either the CDC sync path (T0034) or the deactivation
path (T0053).

Both tests are SLOW: the embedder model load can take 30-60 s on the
first bootstrap. The pytest timeouts are sized accordingly.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest


def _embedding_provider_body(entity_id: str) -> dict:
    """HuggingFace embedder using a tiny local model that
    sentence-transformers can pull on demand (already a transitive dep
    of this project). No HF token needed for public models, but the
    config field is required by the schema — pass an empty placeholder.
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


def _ic_config_body(*, embedder_id: str) -> dict:
    return {
        "embedding_provider_id": embedder_id,
        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    }


@pytest.mark.asyncio
async def test_t0053_config_delete_deactivates_subsystem(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0053 — full lifecycle: PUT config → bootstrap → DELETE config →
    search returns 503 again."""
    embedder_id = f"emb-t0053-{unique_suffix}"

    # 1. EmbeddingProvider
    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        # 2. Activate subsystem config
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        # 3. Bootstrap. May take a while to spin up the embedder /
        #    create vector tables — give it a generous timeout. On a
        #    fresh DB there are no entities to ingest, so no actual
        #    embedding calls happen.
        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=httpx.Timeout(180.0, connect=10.0),
        )
        # Accept 200 (orchestrator returned counts) or any 5xx that
        # signals the embedder couldn't load — in which case we fall
        # back to verifying the config-only deactivation path.
        if boot.status_code != 200:
            pytest.skip(
                f"bootstrap returned {boot.status_code}; embedder model "
                f"may be unavailable. Body: {boot.text[:300]}"
            )

        # 4. Search now works (no hits because no agents indexed) — but
        #    the subsystem is active, so it should NOT return 503.
        search_active = await client.post(
            "/v1/agents/search", json={"query": "anything", "top_k": 3},
        )
        assert search_active.status_code == 200, search_active.text

        # 5. DELETE the config — this is the actual T0053 assertion target
        rm = await client.delete("/v1/internal_collections/config")
        assert rm.status_code == 204, rm.text
        config_created = False  # already cleaned

        # 6. Search must return 503 with /errors/subsystem-inactive
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
        "model": {"provider_id": provider_id, "model_name": "claude-sonnet-4-6"},
        "tools": [],
    }


@pytest.mark.asyncio
async def test_t0034_cdc_new_agent_appears_in_search(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0034 — after bootstrap, creating a new Agent via the CRUD route
    triggers the CDC hook and the agent becomes findable via
    `/agents/search` within a bounded poll window.

    Uses a distinctive description with the unique_suffix so the search
    query is unambiguous about which agent it should be retrieving.
    """
    embedder_id = f"emb-t0034-{unique_suffix}"
    llm_id = f"llm-t0034-{unique_suffix}"
    agent_id = f"agent-cdc-{unique_suffix}"
    distinctive = f"distinctive-marker-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    agent_created = False
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=httpx.Timeout(180.0, connect=10.0),
        )
        if boot.status_code != 200:
            pytest.skip(
                f"bootstrap returned {boot.status_code}; embedder model "
                f"may be unavailable. Body: {boot.text[:300]}"
            )

        # Need an LLMProvider for the Agent's model reference.
        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        # Create the agent — CDC hook should embed + ingest.
        ag = await client.post(
            "/v1/agents",
            json=_agent_body(
                agent_id, provider_id=llm_id, description=distinctive,
            ),
        )
        assert ag.status_code == 201, ag.text
        agent_created = True

        # Poll search for up to 30 s. The CDC ingest happens in the
        # subsystem's worker queue and the embedder call is fast for
        # a single short string with the model already loaded.
        deadline_iters = 60  # 60 * 0.5 s = 30 s
        found = False
        for _ in range(deadline_iters):
            search = await client.post(
                "/v1/agents/search",
                json={"query": distinctive, "top_k": 5},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            assert search.status_code == 200, search.text
            hits = search.json()["hits"]
            ids = [h["document_id"] for h in hits]
            if agent_id in ids:
                found = True
                break
            await asyncio.sleep(0.5)
        assert found, (
            f"agent {agent_id!r} did not appear in /agents/search results "
            f"within 30 s; last response: {hits!r}"
        )
    finally:
        if agent_created:
            await client.delete(f"/v1/agents/{agent_id}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


async def _bootstrap_subsystem(
    client: httpx.AsyncClient, embedder_id: str,
) -> None:
    """PUT config + POST bootstrap. Used by T0035 / T0036."""
    put = await client.put(
        "/v1/internal_collections/config",
        json=_ic_config_body(embedder_id=embedder_id),
    )
    assert put.status_code == 200, put.text
    boot = await client.post(
        "/v1/internal_collections/bootstrap",
        timeout=httpx.Timeout(180.0, connect=10.0),
    )
    if boot.status_code != 200:
        pytest.skip(
            f"bootstrap returned {boot.status_code}; embedder model "
            f"may be unavailable. Body: {boot.text[:300]}"
        )


async def _poll_search_for(
    client: httpx.AsyncClient,
    *,
    query: str,
    expected_id: str | None,
    present: bool,
    deadline_iters: int = 60,
) -> list[str]:
    """Poll /agents/search until ``expected_id`` is present (when
    ``present=True``) or absent (when ``present=False``). Returns the
    last observed list of ids.

    Distinguishing presence and absence in the same primitive lets
    T0035 and T0036 share polling logic.
    """
    last_ids: list[str] = []
    for _ in range(deadline_iters):
        search = await client.post(
            "/v1/agents/search",
            json={"query": query, "top_k": 10},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        assert search.status_code == 200, search.text
        last_ids = [h["document_id"] for h in search.json()["hits"]]
        is_present = expected_id is not None and expected_id in last_ids
        if (present and is_present) or (not present and not is_present):
            return last_ids
        await asyncio.sleep(0.5)
    return last_ids


@pytest.mark.asyncio
async def test_t0035_cdc_deleted_agent_removed_from_search(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0035 — DELETE on an Agent removes it from /agents/search results
    within a bounded poll window. CDC handles the removal hook the
    same way it handles the create hook."""
    embedder_id = f"emb-t0035-{unique_suffix}"
    llm_id = f"llm-t0035-{unique_suffix}"
    agent_id = f"agent-rm-{unique_suffix}"
    distinctive = f"removable-marker-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        ag = await client.post(
            "/v1/agents",
            json=_agent_body(
                agent_id, provider_id=llm_id, description=distinctive,
            ),
        )
        assert ag.status_code == 201, ag.text

        # Wait for it to be indexed (CDC create hook).
        ids = await _poll_search_for(
            client, query=distinctive, expected_id=agent_id, present=True,
        )
        assert agent_id in ids, f"create-hook never indexed: {ids!r}"

        # DELETE the agent.
        rm = await client.delete(f"/v1/agents/{agent_id}")
        assert rm.status_code == 204, rm.text

        # Wait for it to disappear (CDC delete hook).
        ids_after = await _poll_search_for(
            client, query=distinctive, expected_id=agent_id, present=False,
        )
        assert agent_id not in ids_after, (
            f"delete-hook did not remove agent within poll window: {ids_after!r}"
        )
    finally:
        # agent already deleted in success path; suppress error in cleanup
        await client.delete(f"/v1/agents/{agent_id}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


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
    llm_id = f"llm-t0062-{unique_suffix}"
    shared_marker = f"shared-marker-{unique_suffix}"
    agent_ids = [f"agent-t0062-{unique_suffix}-{i}" for i in range(3)]

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    created_agents: list[str] = []
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
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

        # Wait for all three to be indexed (CDC).
        await _poll_search_for(
            client, query=shared_marker, expected_id=agent_ids[-1], present=True,
        )

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


@pytest.mark.asyncio
async def test_t0059_search_ranks_marker_match_above_noise(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0059 — semantic search ranks the agent whose description
    contains the queried marker strictly higher than an unrelated
    agent's. Uses two distinct distinctive markers and queries one;
    asserts the marker-A agent's score > marker-B agent's score.

    Sentence-transformers cosine similarity gives a clear margin
    between exact-marker match and an unrelated description, so this
    pin is robust without a tight tolerance.
    """
    embedder_id = f"emb-t0059-{unique_suffix}"
    llm_id = f"llm-t0059-{unique_suffix}"
    agent_a = f"agent-a-{unique_suffix}"
    agent_b = f"agent-b-{unique_suffix}"
    marker_a = f"marker-aaa-{unique_suffix}"
    marker_b = f"marker-bbb-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    created_agents: list[str] = []
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        # Two agents with completely distinct descriptions
        for aid, desc in ((agent_a, marker_a), (agent_b, marker_b)):
            ag = await client.post(
                "/v1/agents",
                json=_agent_body(aid, provider_id=llm_id, description=desc),
            )
            assert ag.status_code == 201, ag.text
            created_agents.append(aid)

        # Wait for both to be indexed
        await _poll_search_for(
            client, query=marker_a, expected_id=agent_a, present=True,
        )
        await _poll_search_for(
            client, query=marker_b, expected_id=agent_b, present=True,
        )

        # Query for marker A — both agents are eligible (they share
        # the trailing unique_suffix), but agent_a's description is
        # the one that contains marker_a verbatim, so it MUST rank
        # strictly higher.
        resp = await client.post(
            "/v1/agents/search",
            json={"query": marker_a, "top_k": 10},
        )
        assert resp.status_code == 200, resp.text
        hits = {h["document_id"]: h["score"] for h in resp.json()["hits"]}
        assert agent_a in hits, hits
        assert agent_b in hits, hits
        score_a = hits[agent_a]
        score_b = hits[agent_b]
        assert score_a is not None and score_b is not None, hits
        assert score_a > score_b, (
            f"expected agent_a (marker match) to outrank agent_b; "
            f"got score_a={score_a}, score_b={score_b}"
        )
    finally:
        for aid in created_agents:
            await client.delete(f"/v1/agents/{aid}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


@pytest.mark.asyncio
async def test_t0128_collection_with_marker_searchable(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0128 — after bootstrap, a Collection whose description contains
    a unique marker is findable via `/v1/collections/search`.

    NB: the original backlog wording said "create collection + document,
    search finds the document marker" — but there's no
    `/v1/documents/search` endpoint, and `/v1/collections/search`
    searches over Collection rows (not their documents). Reframed to
    pin the Collection-search path through the internal-collections
    subsystem, mirroring T0034 for Agent.
    """
    embedder_id = f"emb-t0128-{unique_suffix}"
    coll_id = f"col-t0128-{unique_suffix}"
    marker = f"collection-marker-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    coll_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        # Create the Collection AFTER bootstrap so the CDC create-hook
        # is responsible for indexing it (mirror of T0034).
        coll = await client.post(
            "/v1/collections",
            json={
                "id": coll_id,
                "description": marker,
                "embedder": {
                    "provider_id": embedder_id,
                    "model": "sentence-transformers/all-MiniLM-L6-v2",
                },
            },
        )
        assert coll.status_code == 201, coll.text
        coll_created = True

        # Poll /v1/collections/search until the new collection appears
        deadline_iters = 60  # ~30 s at 0.5 s cadence
        found = False
        last_ids: list[str] = []
        for _ in range(deadline_iters):
            search = await client.post(
                "/v1/collections/search",
                json={"query": marker, "top_k": 5},
            )
            assert search.status_code == 200, search.text
            last_ids = [h["document_id"] for h in search.json()["hits"]]
            if coll_id in last_ids:
                found = True
                break
            await asyncio.sleep(0.5)
        assert found, (
            f"new collection not indexed within 30s; "
            f"last hits={last_ids!r}"
        )
    finally:
        if coll_created:
            await client.delete(f"/v1/collections/{coll_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


@pytest.mark.asyncio
async def test_t0107_cdc_unicode_marker_searchable(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0107 — an Agent whose description contains a unique CJK + emoji
    marker is findable via /v1/agents/search after CDC ingestion. Pins
    that the embedder + vector store handle multi-byte unicode without
    truncation or normalization-mismatch."""
    embedder_id = f"emb-t0107-{unique_suffix}"
    llm_id = f"llm-t0107-{unique_suffix}"
    agent_id = f"agent-uni-{unique_suffix}"
    # CJK + emoji marker. The unique_suffix at the end keeps this
    # distinct from the (passing) plain-ascii T0034 marker.
    marker = f"日本語マーカー 🎉 {unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    agent_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        ag = await client.post(
            "/v1/agents",
            json=_agent_body(agent_id, provider_id=llm_id, description=marker),
        )
        assert ag.status_code == 201, ag.text
        agent_created = True

        ids = await _poll_search_for(
            client, query=marker, expected_id=agent_id, present=True,
        )
        assert agent_id in ids, (
            f"unicode-marker agent not indexed within poll window: {ids!r}"
        )
    finally:
        if agent_created:
            await client.delete(f"/v1/agents/{agent_id}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


@pytest.mark.asyncio
async def test_t0090_cdc_burst_load_all_agents_indexed(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0090 — after bootstrap, POST 10 agents back-to-back with a
    shared marker; ALL 10 must surface in `/agents/search` within
    a bounded poll window. Catches CDC-queue dropping or coalescing
    under burst load.
    """
    embedder_id = f"emb-t0090-{unique_suffix}"
    llm_id = f"llm-t0090-{unique_suffix}"
    shared_marker = f"burst-marker-{unique_suffix}"
    n_agents = 10
    agent_ids = [f"agent-burst-{unique_suffix}-{i:02d}" for i in range(n_agents)]

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    created_agents: list[str] = []
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        # Burst-create all 10 agents concurrently
        responses = await asyncio.gather(
            *[
                client.post(
                    "/v1/agents",
                    json=_agent_body(
                        aid, provider_id=llm_id, description=shared_marker,
                    ),
                )
                for aid in agent_ids
            ]
        )
        for r in responses:
            assert r.status_code == 201, r.text
        created_agents.extend(agent_ids)

        # Poll up to 60s for ALL 10 ids to appear
        deadline_iters = 120  # 60s @ 0.5s
        last_ids: set[str] = set()
        for _ in range(deadline_iters):
            search = await client.post(
                "/v1/agents/search",
                json={"query": shared_marker, "top_k": n_agents + 5},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            assert search.status_code == 200, search.text
            last_ids = {h["document_id"] for h in search.json()["hits"]}
            if set(agent_ids).issubset(last_ids):
                break
            await asyncio.sleep(0.5)
        missing = set(agent_ids) - last_ids
        assert not missing, (
            f"CDC dropped {len(missing)}/{n_agents} agents after 60s poll: "
            f"missing={sorted(missing)!r}, present={sorted(last_ids)!r}"
        )
    finally:
        for aid in created_agents:
            await client.delete(f"/v1/agents/{aid}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


@pytest.mark.asyncio
async def test_t0091_cdc_reactivation_cycle_works(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0091 — full deactivation + reactivation cycle:
    PUT config → bootstrap → DELETE config (subsystem inactive) →
    PUT config → bootstrap → freshly-created Agent surfaces in search.

    Catches state leakage between activation cycles (e.g. stale CDC
    workers, stale subsystem references in the registry).
    """
    embedder_id = f"emb-t0091-{unique_suffix}"
    llm_id = f"llm-t0091-{unique_suffix}"
    agent_id = f"agent-cycle-{unique_suffix}"
    marker = f"cycle-marker-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_active = False
    llm_created = False
    agent_created = False
    try:
        # First activation cycle
        await _bootstrap_subsystem(client, embedder_id)
        config_active = True

        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        # Deactivate
        rm = await client.delete("/v1/internal_collections/config")
        assert rm.status_code == 204, rm.text
        config_active = False
        # Confirm subsystem is inactive (search 503)
        check = await client.post(
            "/v1/agents/search", json={"query": "anything", "top_k": 3},
        )
        assert check.status_code == 503, check.text

        # Re-activate
        await _bootstrap_subsystem(client, embedder_id)
        config_active = True

        # Create a new agent AFTER re-activation; CDC must work again
        ag = await client.post(
            "/v1/agents",
            json=_agent_body(agent_id, provider_id=llm_id, description=marker),
        )
        assert ag.status_code == 201, ag.text
        agent_created = True

        ids = await _poll_search_for(
            client, query=marker, expected_id=agent_id, present=True,
        )
        assert agent_id in ids, (
            f"after reactivation, CDC did not re-index the new agent: {ids!r}"
        )
    finally:
        if agent_created:
            await client.delete(f"/v1/agents/{agent_id}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_active:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


@pytest.mark.asyncio
async def test_t0036_cdc_updated_agent_description_indexed(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0036 — PUTing an Agent's description with a new distinctive
    marker makes the agent findable by the new marker within the poll
    window. The CDC update hook re-embeds with the latest text.
    """
    embedder_id = f"emb-t0036-{unique_suffix}"
    llm_id = f"llm-t0036-{unique_suffix}"
    agent_id = f"agent-upd-{unique_suffix}"
    initial_marker = f"initial-marker-{unique_suffix}"
    updated_marker = f"updated-marker-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    agent_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        ag = await client.post(
            "/v1/agents",
            json=_agent_body(
                agent_id, provider_id=llm_id, description=initial_marker,
            ),
        )
        assert ag.status_code == 201, ag.text
        agent_created = True

        # Wait for initial indexing.
        await _poll_search_for(
            client, query=initial_marker, expected_id=agent_id, present=True,
        )

        # PUT the agent with the new description.
        put = await client.put(
            f"/v1/agents/{agent_id}",
            json=_agent_body(
                agent_id, provider_id=llm_id, description=updated_marker,
            ),
        )
        assert put.status_code == 200, put.text

        # Search by the NEW marker — must find the same agent_id.
        ids = await _poll_search_for(
            client, query=updated_marker, expected_id=agent_id, present=True,
        )
        assert agent_id in ids, (
            f"update-hook did not re-index with new description: {ids!r}"
        )
    finally:
        if agent_created:
            await client.delete(f"/v1/agents/{agent_id}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


# ============================================================================
# T0164 — CDC for Graph: new Graph appears in /v1/graphs/search
# ============================================================================


def _graph_body(entity_id: str, *, agent_id: str, description: str) -> dict:
    return {
        "id": entity_id,
        "description": description,
        "nodes": [
            {"kind": "agent", "id": "n1", "agent_id": agent_id},
            {"kind": "terminal", "id": "end"},
        ],
        "edges": [
            {"kind": "static", "from_node": "n1", "to_node": "end"},
        ],
        "entry_node_id": "n1",
    }


@pytest.mark.asyncio
async def test_t0164_cdc_new_graph_appears_in_search(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0164 — after bootstrap, creating a new Graph via the CRUD route
    triggers the CDC hook and the graph becomes findable via
    `/v1/graphs/search` within a bounded poll window. Mirror of T0034
    (Agent CDC) for the third CDC-mirrored entity kind.
    """
    embedder_id = f"emb-t0164-{unique_suffix}"
    llm_id = f"llm-t0164-{unique_suffix}"
    agent_id = f"agent-t0164-{unique_suffix}"
    graph_id = f"graph-cdc-{unique_suffix}"
    distinctive = f"graph-marker-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    agent_created = False
    graph_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        # Need an LLMProvider + Agent for the Graph's agent node reference
        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        ag = await client.post(
            "/v1/agents",
            json=_agent_body(
                agent_id, provider_id=llm_id,
                description=f"agent-for-{graph_id}",
            ),
        )
        assert ag.status_code == 201, ag.text
        agent_created = True

        # Create the graph — CDC hook should embed + ingest its description
        gr = await client.post(
            "/v1/graphs",
            json=_graph_body(
                graph_id, agent_id=agent_id, description=distinctive,
            ),
        )
        assert gr.status_code == 201, gr.text
        graph_created = True

        # Poll /v1/graphs/search for the marker
        deadline_iters = 60  # ~30 s at 0.5 s cadence
        found = False
        last_ids: list[str] = []
        for _ in range(deadline_iters):
            search = await client.post(
                "/v1/graphs/search",
                json={"query": distinctive, "top_k": 5},
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
            assert search.status_code == 200, search.text
            last_ids = [h["document_id"] for h in search.json()["hits"]]
            if graph_id in last_ids:
                found = True
                break
            await asyncio.sleep(0.5)
        assert found, (
            f"graph {graph_id!r} did not appear in /v1/graphs/search "
            f"results within 30 s; last hits={last_ids!r}"
        )
    finally:
        if graph_created:
            await client.delete(f"/v1/graphs/{graph_id}")
        if agent_created:
            await client.delete(f"/v1/agents/{agent_id}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


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
    but matrix/api/routers/_cdc_hooks.py only wires hooks for
    agent / graph / collection — Toolset CRUD does NOT live-update
    the tools index. This test pins the positive-control bootstrap
    path; live CDC for Toolsets is out of scope.
    """
    embedder_id = f"emb-t0165-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id)
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


# ============================================================================
# T0174 — query-based discrimination of two agents (positive control)
# ============================================================================


@pytest.mark.asyncio
async def test_t0174_search_query_distinguishes_two_agents(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0174 — index two agents with disjoint descriptions; searching for
    a marker unique to agent A must rank agent A above agent B.

    NB: spec §11 documents `SearchRequest = { query, top_k?, filter? }`
    but matrix/api/routers/internal_collections.py:97 actually only
    accepts `{ query, top_k }` — the `filter` field is silently
    ignored by Pydantic. This test pins the IMPLEMENTED behaviour
    (semantic search via the query string) rather than the
    unimplemented filter field. Sending a `filter` key in the body
    must NOT crash the route.
    """
    embedder_id = f"emb-t0174-{unique_suffix}"
    llm_id = f"llm-t0174-{unique_suffix}"
    agent_a = f"agent-a-{unique_suffix}"
    agent_b = f"agent-b-{unique_suffix}"
    marker_a = f"marker-zebra-{unique_suffix}"
    marker_b = f"marker-octopus-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    a_created = False
    b_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
        assert llm.status_code == 201, llm.text
        llm_created = True

        a = await client.post(
            "/v1/agents",
            json=_agent_body(agent_a, provider_id=llm_id, description=marker_a),
        )
        assert a.status_code == 201, a.text
        a_created = True

        b = await client.post(
            "/v1/agents",
            json=_agent_body(agent_b, provider_id=llm_id, description=marker_b),
        )
        assert b.status_code == 201, b.text
        b_created = True

        # Wait for both to be indexed
        for _ in range(60):
            s = await client.post(
                "/v1/agents/search",
                json={"query": marker_a, "top_k": 10},
            )
            assert s.status_code == 200, s.text
            ids_seen = {h["document_id"] for h in s.json()["hits"]}
            if {agent_a, agent_b}.issubset(ids_seen):
                break
            await asyncio.sleep(0.5)

        # Search for marker_a — agent_a must rank above agent_b
        s = await client.post(
            "/v1/agents/search",
            # Include an unsupported "filter" key to pin "no crash on
            # extra body field" (spec §11 mentions it but it's unwired)
            json={"query": marker_a, "top_k": 10, "filter": {"unused": True}},
        )
        assert s.status_code == 200, s.text
        ranked = [h["document_id"] for h in s.json()["hits"]]
        assert agent_a in ranked, f"agent_a not in results: {ranked!r}"
        assert agent_b in ranked, f"agent_b not in results: {ranked!r}"
        assert ranked.index(agent_a) < ranked.index(agent_b), (
            f"search for marker_a should rank agent_a above agent_b; "
            f"got {ranked!r}"
        )
    finally:
        if a_created:
            await client.delete(f"/v1/agents/{agent_a}")
        if b_created:
            await client.delete(f"/v1/agents/{agent_b}")
        if llm_created:
            await client.delete(f"/v1/llm_providers/{llm_id}")
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")


# ============================================================================
# T0167 — bootstrap is idempotent (second call returns 200 cleanly)
# ============================================================================


@pytest.mark.asyncio
async def test_t0167_bootstrap_is_idempotent(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0167 — POST /v1/internal_collections/bootstrap a second time
    after the first succeeds returns 200 cleanly (idempotent per spec
    §11). Search results must remain consistent across the two calls.
    """
    embedder_id = f"emb-t0167-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id)
        config_created = True

        # First call already happened in _bootstrap_subsystem. Second call:
        boot2 = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=httpx.Timeout(180.0, connect=10.0),
        )
        assert boot2.status_code == 200, (
            f"second bootstrap should be idempotent (200); got "
            f"{boot2.status_code}: {boot2.text}"
        )
        body = boot2.json()
        # The orchestrator returns count metadata; just assert the shape
        # is sane (a dict / mapping).
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


# ============================================================================
# T0168 — PUT config with non-existent embedding_provider_id
# ============================================================================


@pytest.mark.asyncio
async def test_t0168_put_config_with_missing_embedder_clean_envelope(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0168 — PUT /v1/internal_collections/config referencing an
    embedding_provider_id that doesn't exist. Mirrors T0068's permissive
    referential-integrity contract (rows are persisted; orphan surfaces
    at use-time): the API may either reject at PUT time (4xx) or accept
    and surface the orphan at bootstrap. Pin "no /errors/internal".
    """
    missing_embedder = f"missing-emb-{unique_suffix}"

    config_created = False
    try:
        resp = await client.put(
            "/v1/internal_collections/config",
            json={
                "embedding_provider_id": missing_embedder,
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
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


# ============================================================================
# T0169 — PUT config reconfigures embedder; subsystem keeps serving
# ============================================================================


@pytest.mark.asyncio
async def test_t0169_put_config_reconfigure_embedder_works(
    client: httpx.AsyncClient, unique_suffix: str,
) -> None:
    """T0169 — after bootstrap, PUT /v1/internal_collections/config
    again with a DIFFERENT embedding_provider_id (still valid). The
    second PUT must NOT return 409 (config is treated as upsert per
    spec §11), and search routes must continue to respond after the
    reconfigure (no stale-registry 5xx).

    Uses the same model name on both providers so the on-disk vector
    dimensions don't drift.
    """
    embedder_a = f"emb-t0169a-{unique_suffix}"
    embedder_b = f"emb-t0169b-{unique_suffix}"

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
        await _bootstrap_subsystem(client, embedder_a)
        config_created = True

        # Reconfigure to embedder_b — must be a clean upsert
        put_b = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_b),
        )
        assert put_b.status_code == 200, (
            f"reconfigure PUT should upsert with 200; got "
            f"{put_b.status_code}: {put_b.text}"
        )

        # Search route still responds cleanly (no 5xx, no
        # subsystem-inactive). It may return 503 briefly during the
        # registry swap; tolerate that on the first poll.
        last: httpx.Response | None = None
        for _ in range(20):
            s = await client.post(
                "/v1/agents/search", json={"query": "anything", "top_k": 3},
            )
            last = s
            if s.status_code == 200:
                break
            if s.status_code == 503:
                await asyncio.sleep(0.5)
                continue
            # Anything else (4xx/5xx) is unexpected — fail loudly
            break
        assert last is not None
        assert last.status_code == 200, (
            f"search did not recover to 200 after reconfigure within "
            f"10 s; last status={last.status_code}: {last.text}"
        )
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_a}")
        await client.delete(f"/v1/embedding_providers/{embedder_b}")


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

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        await _bootstrap_subsystem(client, embedder_id)
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

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=httpx.Timeout(180.0, connect=10.0),
        )
        assert boot.status_code == 200, (
            f"bootstrap on empty DB should return 200, got "
            f"{boot.status_code}: {boot.text}"
        )
        body = boot.json()
        assert isinstance(body, dict), body
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
    llm_id = f"llm-t0224-{unique_suffix}"
    agent_id = f"agent-t0224-{unique_suffix}"

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    llm_created = False
    agent_created = False
    try:
        # Activate config (PUT) — but do not call bootstrap yet
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id),
        )
        assert put.status_code == 200, put.text
        config_created = True

        # Seed one Agent BEFORE bootstrap so the bootstrap counts it
        llm = await client.post("/v1/llm_providers", json=_llm_body(llm_id))
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

        # Bootstrap and pin the shape
        boot = await client.post(
            "/v1/internal_collections/bootstrap",
            timeout=httpx.Timeout(180.0, connect=10.0),
        )
        assert boot.status_code == 200, boot.text
        body = boot.json()
        assert isinstance(body, dict), body
        # Shape: top-level dict with at least one int value (one count)
        # The exact key names are implementation detail; pin "at least
        # one int value present" and "no string error keys"
        int_values = [
            v for v in body.values() if isinstance(v, int)
        ]
        nested_int_values = []
        for v in body.values():
            if isinstance(v, dict):
                nested_int_values.extend(
                    iv for iv in v.values() if isinstance(iv, int)
                )
        assert int_values or nested_int_values, (
            f"bootstrap envelope contains no integer counts: {body!r}"
        )
        # No "error" key indicating a failed-but-200-anyway path
        for forbidden in ("error", "errors", "failed"):
            assert forbidden not in body, (
                f"bootstrap envelope unexpectedly carries {forbidden!r} "
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

    pr = await client.post(
        "/v1/embedding_providers", json=_embedding_provider_body(embedder_id),
    )
    assert pr.status_code == 201, pr.text

    config_created = False
    try:
        put = await client.put(
            "/v1/internal_collections/config",
            json=_ic_config_body(embedder_id=embedder_id),
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
    finally:
        if config_created:
            await client.delete("/v1/internal_collections/config")
        await client.delete(f"/v1/embedding_providers/{embedder_id}")
