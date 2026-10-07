"""The ``delete_agent`` tool tells the agent what deleting the operator or builder does before it acts (ADM-13).

Setup completeness is derived live (``primer/bootstrap/setup_state.py``): with the seeded ``operator`` or ``builder`` row gone,
``GET /v1/auth/status`` reports ``setup_complete: false``, admins are sent to the setup checklist and every other user waits on a
setup screen. The console warns before it deletes either one (``tests/ui/test_platform_delete_confirm.py``). The lead ruled that the
SERVER does not refuse (a restart or Re-run seed is the designed way back) but that the system tool's descriptor carries the same
facts, so an agent reads them before it acts.

Negative controls: no other tool says it (a graph, a toolset, the agent's own update), and the tool still deletes the operator.
"""

from __future__ import annotations

import pytest

from primer.bootstrap.defaults import RESERVED_BUILDER_AGENT, RESERVED_OPERATOR_AGENT
from primer.model.agent import Agent
from tests.toolset.test_system import _agent
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401  (world is a fixture)


async def _description(toolset, tool_id: str) -> str:
    descriptions = {tool.id: tool.description async for tool in toolset.list_tools()}
    return descriptions[tool_id]


@pytest.mark.asyncio
async def test_delete_agent_names_the_two_agents_and_what_deleting_them_does(world) -> None:
    _, toolset, _ = world

    text = await _description(toolset, "delete_agent")

    assert RESERVED_OPERATOR_AGENT in text and RESERVED_BUILDER_AGENT in text, "it must name the two agents"
    assert "not set up" in text, "it must say the install counts as not set up"
    assert "setup checklist" in text and "waits" in text, "it must say who is sent where"
    assert "POST /v1/setup/seed" in text, "it must say how the agent comes back"
    assert "default definition" in text, "it must say the re-created agent loses the operator's edits"


@pytest.mark.parametrize(
    "tool_id",
    ["get_agent", "find_agents", "create_agent", "update_agent", "delete_graph", "delete_toolset", "delete_collection", "delete_model_profile"],
)
@pytest.mark.asyncio
async def test_no_other_tool_carries_the_setup_note(world, tool_id) -> None:
    _, toolset, _ = world

    text = await _description(toolset, tool_id)

    assert "not set up" not in text, f"{tool_id} cannot reopen setup, so the note does not apply"


@pytest.mark.parametrize("agent_id", [RESERVED_OPERATOR_AGENT, RESERVED_BUILDER_AGENT])
@pytest.mark.asyncio
async def test_the_tool_still_deletes_a_seeded_agent(world, agent_id) -> None:
    """The ruling is a descriptor note, not a refusal."""
    sp, toolset, _ = world
    row = _agent()
    await sp.get_storage(Agent).create(Agent.model_validate({**row.model_dump(mode="json"), "id": agent_id}))

    is_error, answer = await _call(toolset, "delete_agent", id=agent_id)

    assert not is_error and answer == {"deleted": True, "id": agent_id}
    assert await sp.get_storage(Agent).get(agent_id) is None
