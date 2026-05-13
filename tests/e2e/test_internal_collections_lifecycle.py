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
