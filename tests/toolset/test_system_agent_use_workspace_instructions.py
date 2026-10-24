"""``Agent.use_workspace_instructions`` through the agent tools: the ``system`` toolset and the builder's ``crud`` toolset (pivot P2, Task 16).

Both surfaces build their ``create_agent`` / ``update_agent`` schemas from the ``Agent`` model, so an agent (the builder, an operator using
``call_tool``) sees the flag, its default and what it does, and can set it. Run on a real SQLite store, as the other agent tool suites are;
``tests/api/test_agent_use_workspace_instructions.py`` covers the route and a row stored before the field existed.
"""

from __future__ import annotations

import pytest

from primer.model.agent import Agent
from primer.toolset.crud import build_crud_toolset
from tests._support.agent_check_rows import agent_body, profile_row
from tests.api.test_agent_use_workspace_instructions import strip_the_key
from tests.toolset.test_system_agent_reference_checks import SURFACES, _call_surface, _store
from tests.toolset.test_system_crud_guards import world  # noqa: F401  (world is a fixture)


async def _tools(sp, toolset, surface: str) -> dict:
    provider = toolset if surface == "system" else build_crud_toolset(storage_provider=sp)
    return {tool.id: tool async for tool in provider.list_tools()}


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("name", ["create_agent", "update_agent"])
async def test_the_write_tools_describe_the_flag(world, surface: str, name: str) -> None:
    sp, toolset, _ = world
    schema = (await _tools(sp, toolset, surface))[name].args_schema

    field = schema["$defs"]["Agent"]["properties"]["use_workspace_instructions"]

    assert field["type"] == "boolean" and field["default"] is True
    assert "agents.md" in field["description"]
    assert "use_workspace_instructions" not in schema["$defs"]["Agent"].get("required", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_an_agent_created_off_is_stored_and_read_back_off(world, surface: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))

    is_error, answer = await _call_surface(
        surface, sp, toolset, "create_agent", entity={**agent_body("reviewer"), "use_workspace_instructions": False},
    )

    assert not is_error, answer
    assert (await sp.get_storage(Agent).get("reviewer")).use_workspace_instructions is False
    is_error, got = await _call_surface("system", sp, toolset, "get_agent", id="reviewer")
    assert not is_error and got["use_workspace_instructions"] is False, got


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_an_update_of_the_row_it_read_keeps_the_flag_and_can_change_it(world, surface: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))
    await _store(sp, Agent.model_validate({**agent_body("reviewer"), "use_workspace_instructions": False}))
    _, read = await _call_surface("system", sp, toolset, "get_agent", id="reviewer")

    is_error, answer = await _call_surface(surface, sp, toolset, "update_agent", id="reviewer", entity={**read, "description": "edited"})
    assert not is_error, answer
    assert (await sp.get_storage(Agent).get("reviewer")).use_workspace_instructions is False

    is_error, answer = await _call_surface(
        surface, sp, toolset, "update_agent", id="reviewer", entity={**read, "use_workspace_instructions": True},
    )
    assert not is_error, answer
    assert (await sp.get_storage(Agent).get("reviewer")).use_workspace_instructions is True


@pytest.mark.asyncio
async def test_a_row_stored_before_the_field_existed_reads_on_through_the_tools(world) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))
    await _store(sp, Agent.model_validate({**agent_body("old-agent"), "use_workspace_instructions": False}))
    await strip_the_key(sp, "old-agent")

    is_error, got = await _call_surface("system", sp, toolset, "get_agent", id="old-agent")

    assert not is_error and got["use_workspace_instructions"] is True, got
