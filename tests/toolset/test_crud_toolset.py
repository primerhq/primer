"""S5 P1: the ``crud`` toolset is the builder's construction surface."""
from __future__ import annotations

import json

from primer.model.agent import Agent
from primer.toolset.crud import (
    CRUD_TOOL_NAMES,
    CRUD_TOOLSET_ID,
    build_crud_toolset,
)


async def test_exposes_exactly_the_construction_tools(fake_storage_provider):
    provider = build_crud_toolset(storage_provider=fake_storage_provider)
    ids = {t.id async for t in provider.list_tools()}
    assert ids == set(CRUD_TOOL_NAMES)


async def test_every_tool_is_scoped_and_self_describing(fake_storage_provider):
    provider = build_crud_toolset(storage_provider=fake_storage_provider)
    async for tool in provider.list_tools():
        assert tool.toolset_id == CRUD_TOOLSET_ID, tool.id
        assert "Use when" in tool.description, tool.id


async def test_create_agent_writes_a_row(fake_storage_provider):
    provider = build_crud_toolset(storage_provider=fake_storage_provider)
    res = await provider.call(
        tool_name="create_agent",
        arguments={
            "entity": {
                "id": "agent-x",
                "description": "built by the builder",
                "model": {"profile_id": "prov--m"},
            }
        },
    )
    assert res.is_error is False, res.output
    row = await fake_storage_provider.get_storage(Agent).get("agent-x")
    assert row is not None and row.description == "built by the builder"


async def test_update_agent_replaces_the_row(fake_storage_provider):
    provider = build_crud_toolset(storage_provider=fake_storage_provider)
    await provider.call(
        tool_name="create_agent",
        arguments={
            "entity": {
                "id": "agent-y",
                "description": "first",
                "model": {"profile_id": "prov--m"},
            }
        },
    )
    res = await provider.call(
        tool_name="update_agent",
        arguments={
            "id": "agent-y",
            "entity": {
                "id": "agent-y",
                "description": "second",
                "model": {"profile_id": "prov--m"},
            },
        },
    )
    assert res.is_error is False, res.output
    row = await fake_storage_provider.get_storage(Agent).get("agent-y")
    assert row.description == "second"


async def test_trigger_tools_carry_their_long_names(fake_storage_provider):
    provider = build_crud_toolset(storage_provider=fake_storage_provider)
    ids = {t.id async for t in provider.list_tools()}
    assert {"create_trigger", "update_trigger"} <= ids
    assert "create" not in ids and "update" not in ids


async def test_unknown_agent_update_is_a_typed_error(fake_storage_provider):
    provider = build_crud_toolset(storage_provider=fake_storage_provider)
    res = await provider.call(
        tool_name="update_agent",
        arguments={
            "id": "agent-nope",
            "entity": {
                "id": "agent-nope",
                "description": "x",
                "model": {"profile_id": "prov--m"},
            },
        },
    )
    assert res.is_error is True
    assert json.loads(res.output)["type"] == "not-found"
