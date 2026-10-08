"""The system ``create_agent`` / ``update_agent`` tools and the builder's ``crud`` toolset refuse what the REST route refuses (A-09, the
create/update half).

``tests/api/test_agent_reference_checks.py`` covers the route. The generic tools re-implement the verbs, and the BUILDER agent is the
principal most likely to get a profile or toolset id wrong, so it matters that BOTH tool surfaces that create agents (the ``system``
toolset and the ``crud`` toolset, which re-homes ``create_agent`` / ``update_agent`` for the builder) run the same check. The check is
one function (``primer/agent/agent_checks.py``); this runs it through each surface on a real SQLite store and requires the answer to be the
same sentence the route gives, with the field path in front as every other validation answer has it.
"""

from __future__ import annotations

import pytest

from primer.model.agent import Agent
from primer.toolset.crud import build_crud_toolset
from tests._support.agent_check_rows import (
    agent_body,
    profile_missing_message,
    profile_row,
    toolset_row,
    toolsets_missing_message,
)
from tests._support.caller import ADMIN_CALLER
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401  (world is a fixture)

SURFACES = ["system", "crud"]


async def _call_surface(surface: str, sp, system_toolset, name: str, **args):
    """``(is_error, answer)`` for one tool on the ``system`` toolset or on the builder's ``crud`` toolset."""
    if surface == "system":
        return await _call(system_toolset, name, **args)
    import json

    crud = build_crud_toolset(storage_provider=sp)
    result = await crud.call(tool_name=name, arguments=args, ctx=ADMIN_CALLER)
    try:
        return result.is_error, json.loads(result.output)
    except ValueError:
        return result.is_error, result.output


async def _store(sp, row) -> None:
    await sp.get_storage(type(row)).create(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_an_agent_naming_a_missing_profile_is_refused_and_not_stored(world, surface: str) -> None:
    sp, toolset, _ = world

    is_error, answer = await _call_surface(surface, sp, toolset, "create_agent", entity=agent_body("ag-1", profile_id="ghost-profile"))

    assert is_error and answer["type"] == "validation-error", answer
    assert answer["message"] == f"model.profile_id: {profile_missing_message('ghost-profile')}"
    assert await sp.get_storage(Agent).get("ag-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_an_agent_with_tools_of_missing_toolsets_is_refused_naming_each_once(world, surface: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))

    is_error, answer = await _call_surface(
        surface, sp, toolset, "create_agent", entity=agent_body("ag-1", tools=["zeta__a", "alpha__a", "zeta__b"]),
    )

    assert is_error and answer["type"] == "validation-error", answer
    assert answer["message"] == f"tools: {toolsets_missing_message('alpha', 'zeta')}"
    assert await sp.get_storage(Agent).get("ag-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_built_in_and_stored_toolsets_and_existing_profiles_are_accepted(world, surface: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))
    await _store(sp, toolset_row("my-ts"))

    is_error, answer = await _call_surface(
        surface, sp, toolset, "create_agent",
        entity=agent_body("ag-1", tools=["system__list_agents", "_system__list_agents", "web__web_fetch", "my-ts__do_it"]),
    )

    assert not is_error, answer
    assert await sp.get_storage(Agent).get("ag-1") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_an_update_that_adds_a_tool_of_a_missing_toolset_is_refused_and_leaves_the_row(world, surface: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))
    await _store(sp, Agent.model_validate(agent_body("ag-1")))

    is_error, answer = await _call_surface(
        surface, sp, toolset, "update_agent", id="ag-1", entity=agent_body("ag-1", tools=["ghost-ts__x"]),
    )

    assert is_error and answer["type"] == "validation-error", answer
    assert answer["message"] == f"tools: {toolsets_missing_message('ghost-ts')}"
    assert (await sp.get_storage(Agent).get("ag-1")).tools == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_an_update_that_points_the_agent_at_a_missing_profile_is_refused(world, surface: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))
    await _store(sp, Agent.model_validate(agent_body("ag-1")))

    is_error, answer = await _call_surface(
        surface, sp, toolset, "update_agent", id="ag-1", entity=agent_body("ag-1", profile_id="ghost-profile"),
    )

    assert is_error and answer["message"] == f"model.profile_id: {profile_missing_message('ghost-profile')}", answer
    assert (await sp.get_storage(Agent).get("ag-1")).model.profile_id == "mp-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_an_agent_that_already_names_a_deleted_toolset_can_still_be_edited(world, surface: str) -> None:
    """Only a reference the update ADDS is refused: a dangling one that was already there does not trap the agent."""
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))
    await _store(sp, Agent.model_validate(agent_body("ag-1", tools=["gone-ts__x"])))
    edited = {**agent_body("ag-1", tools=["gone-ts__x"]), "description": "edited"}

    is_error, answer = await _call_surface(surface, sp, toolset, "update_agent", id="ag-1", entity=edited)

    assert not is_error, answer
    assert (await sp.get_storage(Agent).get("ag-1")).description == "edited"
