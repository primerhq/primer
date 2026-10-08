"""The system ``create_agent`` / ``update_agent`` tools and the builder's ``crud`` toolset refuse a NEW agent whose id is not a name or whose
description is blank, as ``POST /v1/agents`` does (ticket 01a11c1c).

``tests/api/test_agent_id_and_description_rule.py`` covers the route and the reasoning. The BUILDER agent is the principal most likely to
invent an id like ``Refund Triage``, so both tool surfaces that create agents run the same check and answer the same sentence with the
field in front (``id: ...``, ``description: ...``), type ``validation-error``.
"""

from __future__ import annotations

import pytest

from primer.model.agent import Agent
from tests._support.agent_check_rows import AGENT_DESCRIPTION_MESSAGE, agent_body, agent_id_message, profile_row
from tests.toolset.test_system_agent_reference_checks import SURFACES, _call_surface, _store
from tests.toolset.test_system_crud_guards import world  # noqa: F401  (world is a fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("agent_id", ["Bad Name!", "Refund Triage", "-lead", "a/b", "a" * 64, "acme__assistant"])
async def test_a_new_agent_whose_id_is_not_a_name_is_refused_and_not_stored(world, surface: str, agent_id: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))

    is_error, answer = await _call_surface(surface, sp, toolset, "create_agent", entity=agent_body(agent_id))

    assert is_error and answer["type"] == "validation-error", answer
    assert answer["message"] == f"id: {agent_id_message(agent_id)}"
    assert await sp.get_storage(Agent).get(agent_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_a_new_agent_with_a_name_for_an_id_is_created(world, surface: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))

    is_error, answer = await _call_surface(surface, sp, toolset, "create_agent", entity=agent_body("refund-triage"))

    assert not is_error, answer
    assert await sp.get_storage(Agent).get("refund-triage") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("description", ["", "   ", "\n"])
async def test_a_new_agent_with_a_blank_description_is_refused_and_not_stored(world, surface: str, description: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))

    is_error, answer = await _call_surface(
        surface, sp, toolset, "create_agent", entity={**agent_body("ag-1"), "description": description},
    )

    assert is_error and answer["type"] == "validation-error", answer
    assert answer["message"] == f"description: {AGENT_DESCRIPTION_MESSAGE}"
    assert await sp.get_storage(Agent).get("ag-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_the_id_is_reported_before_the_description_and_the_references(world, surface: str) -> None:
    sp, toolset, _ = world

    is_error, answer = await _call_surface(
        surface, sp, toolset, "create_agent",
        entity={**agent_body("Bad Name!", profile_id="ghost-profile"), "description": " "},
    )

    assert is_error and answer["message"].startswith("id: "), answer


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_an_update_that_makes_the_description_blank_is_refused_and_leaves_the_row(world, surface: str) -> None:
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))
    await _store(sp, Agent.model_validate(agent_body("ag-1")))

    is_error, answer = await _call_surface(
        surface, sp, toolset, "update_agent", id="ag-1", entity={**agent_body("ag-1"), "description": "  "},
    )

    assert is_error and answer["message"] == f"description: {AGENT_DESCRIPTION_MESSAGE}", answer
    assert (await sp.get_storage(Agent).get("ag-1")).description == "a test agent"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_an_agent_stored_before_the_rule_can_still_be_edited(world, surface: str) -> None:
    """A stored ``Legacy_Name`` with a blank description: the update checks neither the id (locked) nor a blank it did not add."""
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))
    await _store(sp, Agent.model_validate({**agent_body("Legacy_Name"), "description": ""}))

    is_error, answer = await _call_surface(
        surface, sp, toolset, "update_agent", id="Legacy_Name",
        entity={**agent_body("Legacy_Name"), "description": "", "temperature": 0.5},
    )

    assert not is_error, answer
    assert (await sp.get_storage(Agent).get("Legacy_Name")).temperature == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("tool", ["create_agent", "update_agent"])
async def test_the_write_descriptors_tell_the_agent_the_rule_before_it_writes(world, surface: str, tool: str) -> None:
    """The model reads the descriptor first, so it should not have to learn the rule from a refusal."""
    from primer.toolset.crud import build_crud_toolset

    sp, toolset, _ = world
    provider = toolset if surface == "system" else build_crud_toolset(storage_provider=sp)

    described = {t.id: t.description async for t in provider.list_tools()}

    assert "lowercase letters, digits, hyphens and single underscores" in described[tool], described[tool]
    assert "must not be blank" in described[tool], described[tool]
    assert "reserved for the agents a harness installs" in described[tool], described[tool]


@pytest.mark.asyncio
async def test_an_agent_stored_with_an_older_id_can_be_deleted_through_the_system_tool(world) -> None:
    """Delete is not a create: ``Legacy_Name`` and a pre-reservation ``old__style`` go away like any other agent."""
    sp, toolset, _ = world
    await _store(sp, profile_row("mp-1"))
    await _store(sp, Agent.model_validate(agent_body("Legacy_Name")))
    await _store(sp, Agent.model_validate(agent_body("old__style")))

    for agent_id in ("Legacy_Name", "old__style"):
        is_error, answer = await _call_surface("system", sp, toolset, "delete_agent", id=agent_id)
        assert not is_error, (agent_id, answer)
        assert await sp.get_storage(Agent).get(agent_id) is None
