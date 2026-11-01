"""GS-5: the LLM provider and model profile deletes enforce the references their save doors enforce."""

from __future__ import annotations

from primer.model.graph import Graph
from primer.model.model_profile import ModelProfile
from tests._support.model_profiles import seed_profile
from tests.api.test_model_profiles_router import _agent, _seed_provider


async def test_llm_provider_delete_stays_permissive_and_the_agent_status_reports_it(client, app) -> None:
    """The e2e suite pins this as the design (t0344, t0265, t0033): a provider is deleted whatever names it; profiles and agents stay and report the break."""
    await _seed_provider(client, "gap-prov")
    pid = await seed_profile(client, "gap-prov", "model-a")
    assert (await client.post("/v1/agents", json=_agent("gap-agent", pid))).status_code == 201

    assert (await client.delete("/v1/llm_providers/gap-prov")).status_code == 204

    assert (await app.state.storage_provider.get_storage(ModelProfile).get(pid)) is not None
    assert (await client.get("/v1/agents/gap-agent")).status_code == 200
    status = (await client.get("/v1/agents/gap-agent/status")).json()
    assert status["ok"] is False and any("gap-prov" in str(issue) for issue in status["issues"]), status


async def test_profile_delete_is_refused_while_a_graph_node_overrides_with_it(client, app) -> None:
    await _seed_provider(client, "gap-prov2")
    default_pid = await seed_profile(client, "gap-prov2", "model-default")
    override_pid = await seed_profile(client, "gap-prov2", "model-override")
    assert (await client.post("/v1/agents", json=_agent("gap-agent2", default_pid))).status_code == 201
    graph = {
        "id": "gap-graph", "description": "override",
        "nodes": [
            {"kind": "begin", "id": "start"},
            {"kind": "agent", "id": "n1", "agent_id": "gap-agent2", "profile_id": override_pid},
            {"kind": "end", "id": "finish"},
        ],
        "edges": [
            {"kind": "static", "from_node": "start", "to_node": "n1"},
            {"kind": "static", "from_node": "n1", "to_node": "finish"},
        ],
    }
    assert (await client.post("/v1/graphs", json=graph)).status_code == 201

    deleted = await client.delete(f"/v1/model_profiles/{override_pid}")

    assert deleted.status_code == 409 and "in_use_by" in deleted.text
    g = await app.state.storage_provider.get_storage(Graph).get("gap-graph")
    assert any(getattr(n, "profile_id", None) == override_pid for n in g.nodes)
