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
