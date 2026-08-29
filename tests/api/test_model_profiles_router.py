"""ModelProfile list-route reference counts (platform wave P2, #19).

GET /v1/model_profiles enriches each item with agent_count/
graph_node_count so a profile card can render "bound by N agents" from
one fetch, without a per-id endpoint or an N+1 query per row.
"""

from __future__ import annotations

import pytest

from pydantic import SecretStr

from primer.model.agent import Agent, AgentModel
from primer.model.provider import AnthropicConfig, Limits, LLMProvider, LLMProviderType
from tests._support.model_profiles import profile_body, seed_profile


def _agent(agent_id: str, profile_id: str, **overrides) -> dict:
    body = dict(
        id=agent_id,
        description="test agent",
        model=AgentModel(profile_id=profile_id),
        temperature=0.0,
        tools=[],
        system_prompt=["you are a test"],
    )
    body.update(overrides)
    return Agent(**body).model_dump(mode="json")


async def _seed_provider(client, provider_id: str) -> None:
    body = LLMProvider(
        id=provider_id,
        provider=LLMProviderType.ANTHROPIC,
        config=AnthropicConfig(api_key=SecretStr("sk-test")),
        limits=Limits(max_concurrency=4),
    ).model_dump(mode="json")
    r = await client.post("/v1/llm_providers", json=body)
    assert r.status_code in (200, 201), r.text


@pytest.mark.asyncio
async def test_unreferenced_profile_reads_zero_counts(client) -> None:
    await _seed_provider(client, "p2-prov-unbound")
    pid = await seed_profile(client, "p2-prov-unbound", "model-a")

    listed = await client.get("/v1/model_profiles?limit=200&offset=0")
    assert listed.status_code == 200
    by_id = {row["id"]: row for row in listed.json()["items"]}
    assert by_id[pid]["agent_count"] == 0
    assert by_id[pid]["graph_node_count"] == 0


@pytest.mark.asyncio
async def test_referenced_profile_counts_binding_agents(client) -> None:
    await _seed_provider(client, "p2-prov-bound")
    pid = await seed_profile(client, "p2-prov-bound", "model-b")
    other_pid = await seed_profile(client, "p2-prov-bound", "model-other")

    # Two agents default-bind to pid; one binds to a different profile
    # entirely, so it must not be tallied against pid.
    for agent_id in ("p2-agent-1", "p2-agent-2"):
        r = await client.post("/v1/agents", json=_agent(agent_id, pid))
        assert r.status_code == 201, r.text
    r = await client.post(
        "/v1/agents", json=_agent("p2-agent-other", other_pid),
    )
    assert r.status_code == 201, r.text

    listed = await client.get("/v1/model_profiles?limit=200&offset=0")
    by_id = {row["id"]: row for row in listed.json()["items"]}
    assert by_id[pid]["agent_count"] == 2
    assert by_id[pid]["graph_node_count"] == 0
    assert by_id[other_pid]["agent_count"] == 1


@pytest.mark.asyncio
async def test_graph_node_override_counts_separately_from_agent_default(
    client,
) -> None:
    """A graph node's own profile_id override is tallied into
    graph_node_count, not agent_count - the two are distinct bindings on
    purpose (#19: "include what the reference distinguishes")."""
    await _seed_provider(client, "p2-prov-graph")
    default_pid = await seed_profile(client, "p2-prov-graph", "model-default")
    override_pid = await seed_profile(client, "p2-prov-graph", "model-override")

    r = await client.post(
        "/v1/agents", json=_agent("p2-agent-graph", default_pid),
    )
    assert r.status_code == 201, r.text

    graph_body = {
        "id": "p2-graph-1",
        "description": "override test",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {
                "kind": "agent",
                "id": "decider",
                "agent_id": "p2-agent-graph",
                "profile_id": override_pid,
            },
            {"kind": "end", "id": "finish"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "decider"},
            {"kind": "static", "from_node": "decider", "to_node": "finish"},
        ],
    }
    r = await client.post("/v1/graphs", json=graph_body)
    assert r.status_code == 201, r.text

    listed = await client.get("/v1/model_profiles?limit=200&offset=0")
    by_id = {row["id"]: row for row in listed.json()["items"]}
    assert by_id[default_pid]["agent_count"] == 1
    assert by_id[default_pid]["graph_node_count"] == 0
    assert by_id[override_pid]["agent_count"] == 0
    assert by_id[override_pid]["graph_node_count"] == 1


@pytest.mark.asyncio
async def test_search_route_also_carries_reference_counts(client) -> None:
    """The q= substring-search branch of the list route shares the same
    enrichment seam as the plain list, not a second code path."""
    await _seed_provider(client, "p2-prov-search")
    pid = await seed_profile(client, "p2-prov-search", "searchable-model-xyz")
    r = await client.post(
        "/v1/agents", json=_agent("p2-agent-search", pid),
    )
    assert r.status_code == 201, r.text

    body = profile_body("p2-prov-search", "searchable-model-xyz")
    listed = await client.get(
        "/v1/model_profiles", params={"q": "searchable-model-xyz"},
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert any(row["id"] == body["id"] for row in items)
    by_id = {row["id"]: row for row in items}
    assert by_id[pid]["agent_count"] == 1
