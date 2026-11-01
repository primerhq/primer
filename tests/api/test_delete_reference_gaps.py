"""GS-5: the LLM provider and model profile deletes enforce the references their save doors enforce."""

from __future__ import annotations

from primer.model.graph import Graph
from primer.model.model_profile import ModelProfile
from tests._support.model_profiles import seed_profile
from tests.api.test_model_profiles_router import _agent, _seed_provider


async def test_llm_provider_delete_is_refused_while_one_of_its_profiles_is_referenced(client, app) -> None:
    await _seed_provider(client, "gap-prov")
    pid = await seed_profile(client, "gap-prov", "model-a")
    assert (await client.post("/v1/agents", json=_agent("gap-agent", pid))).status_code == 201

    deleted = await client.delete("/v1/llm_providers/gap-prov")

    assert deleted.status_code == 409 and "in_use_by" in deleted.text and pid in deleted.text and "gap-agent" in deleted.text
    assert (await app.state.storage_provider.get_storage(ModelProfile).get(pid)) is not None
    assert (await client.get("/v1/llm_providers/gap-prov")).status_code == 200


async def test_llm_provider_delete_takes_its_unreferenced_profiles_with_it(client, app) -> None:
    await _seed_provider(client, "gap-cascade")
    first = await seed_profile(client, "gap-cascade", "model-a")
    second = await seed_profile(client, "gap-cascade", "model-b")

    assert (await client.delete("/v1/llm_providers/gap-cascade")).status_code == 204

    profiles = app.state.storage_provider.get_storage(ModelProfile)
    assert await profiles.get(first) is None and await profiles.get(second) is None
    assert (await client.get("/v1/llm_providers/gap-cascade")).status_code == 404


async def test_one_referenced_profile_keeps_all_of_them(client, app) -> None:
    await _seed_provider(client, "gap-keep")
    used = await seed_profile(client, "gap-keep", "model-a")
    spare = await seed_profile(client, "gap-keep", "model-b")
    assert (await client.post("/v1/agents", json=_agent("gap-keeper", used))).status_code == 201

    assert (await client.delete("/v1/llm_providers/gap-keep")).status_code == 409
    assert (await app.state.storage_provider.get_storage(ModelProfile).get(spare)) is not None


async def test_unreferenced_llm_provider_delete_still_answers_204(client) -> None:
    await _seed_provider(client, "gap-free")
    assert (await client.delete("/v1/llm_providers/gap-free")).status_code == 204


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
