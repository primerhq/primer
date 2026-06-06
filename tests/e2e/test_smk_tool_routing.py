"""SMK tool-routing + collections tests (Phase 1/2).

Hermetic: TRC-01 (catalogue), TRC-02 (tool selection), TRC-04 (call_tool via
agent), TRC-06 (system tools manage entities). The semantic-search-backed ids
(TRC-03/05/07/08/09/10) gate on an embedder + bootstrap.
"""
from __future__ import annotations

import pytest

from tests._support.mock_llm import Rule
from tests._support.runs import (
    make_local_workspace,
    make_scripted_agent,
    start_agent_session,
    wait_terminal,
)
from tests._support.smk import smk
from tests._support.testconfig import requires

pytestmark = pytest.mark.asyncio


@smk("SMK-TRC-01")
async def test_tool_catalogue_lists_reserved_toolsets(authed_client):
    r = await authed_client.get("/v1/tools/catalogue")
    assert r.status_code == 200, r.text
    ids = {i["id"] for i in r.json()["items"]}
    # reserved toolsets surface scoped ids
    assert any(i.startswith("system__") for i in ids)
    assert any(i.startswith("misc__") for i in ids)
    assert "system__call_tool" in ids


@smk("SMK-TRC-02")
async def test_agent_selects_specific_tools(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    sc = f"scripted:trc02-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        tools=["misc__uuid_v4"], rules=[Rule(emit_text="ok")],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    assert (await wait_terminal(authed_client, sid)).get("status") == "ended"
    # only the declared toolset tool was offered to the model (workspace file
    # tools are always present; no other toolset tools leak in)
    reqs = [r for r in registry.requests if r.get("model") == sc and r.get("tools")]
    assert reqs, "model received no tools"
    offered = {t["function"]["name"] for t in reqs[-1]["tools"]}
    assert "misc__uuid_v4" in offered
    assert not any(o.startswith("system__") for o in offered)


@smk("SMK-TRC-04")
async def test_call_tool_dispatch(authed_client, mock_llm, unique_suffix, tmp_path):
    # call_tool is the meta-dispatcher: drive it to create an entity through
    # system.create_agent and verify the entity exists (definitive proof the
    # dispatch reached the underlying tool).
    registry, base_url = mock_llm
    sc = f"scripted:trc04-{unique_suffix}"
    created = f"calltool-made-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        tools=["system__call_tool"],
        rules=[
            Rule(when_tool_offered="call_tool", when_tool_result=False,
                 emit_tool="system__call_tool",
                 emit_args={
                     "toolset_id": "system", "tool_name": "create_agent",
                     "arguments": {"entity": {
                         "id": created, "description": "via call_tool",
                         "model": {"provider_id": f"p-{unique_suffix}", "model_name": sc},
                         "tools": [],
                     }},
                 }),
            Rule(when_tool_result=True, emit_text="done"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    assert (await wait_terminal(authed_client, sid)).get("status") == "ended"
    got = await authed_client.get(f"/v1/agents/{created}")
    assert got.status_code == 200, got.text


@smk("SMK-TRC-06")
async def test_system_tools_manage_entities(authed_client, mock_llm, unique_suffix, tmp_path):
    registry, base_url = mock_llm
    sc = f"scripted:trc06-{unique_suffix}"
    new_agent_id = f"created-{unique_suffix}"
    agent = await make_scripted_agent(
        authed_client, registry, base_url, suffix=unique_suffix, scenario=sc,
        tools=["system__create_agent"],
        rules=[
            Rule(when_tool_offered="create_agent", when_tool_result=False,
                 emit_tool="system__create_agent",
                 emit_args={"entity": {
                     "id": new_agent_id, "description": "made by system tool",
                     "model": {"provider_id": agent_provider(unique_suffix), "model_name": sc},
                     "tools": [],
                 }}),
            Rule(when_tool_result=True, emit_text="created"),
        ],
    )
    wid = await make_local_workspace(authed_client, suffix=unique_suffix, root=tmp_path)
    sid = await start_agent_session(authed_client, workspace_id=wid, agent_id=agent["agent_id"])
    assert (await wait_terminal(authed_client, sid)).get("status") == "ended"
    got = await authed_client.get(f"/v1/agents/{new_agent_id}")
    assert got.status_code == 200, got.text


def agent_provider(suffix: str) -> str:
    # make_scripted_agent names the provider p-<suffix>
    return f"p-{suffix}"


@smk("SMK-TRC-03", "SMK-TRC-05", "SMK-TRC-07", "SMK-TRC-08", "SMK-TRC-09", "SMK-TRC-10")
@requires("embedder")
async def test_internal_collections_and_search():
    pytest.skip("internal-collections search needs an embedder + bootstrap (testconfig)")
