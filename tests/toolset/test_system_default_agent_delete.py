"""The system ``delete_agent`` tool refuses to delete the system default agent, as the REST route does (A-09 follow-up).

The route (``tests/api/test_agent_default_delete.py``) answers 409 ``in_use_by`` while the agent is ``SystemState.default_agent_id``, except for
the seeded ``operator``. The generic delete tool re-implements the verb, so an agent could delete what the route refuses. This runs the same
cases through the tool on a real SQLite store (where ``get_system_state`` and ``set_default_agent_id`` are the real table), and requires the
tool's message to be the route's detail word for word.
"""

from __future__ import annotations

import pytest

from primer.bootstrap.defaults import RESERVED_OPERATOR_AGENT
from primer.model.agent import Agent
from tests._support.compute_rows import agent_row, default_agent_detail, graph_naming_agent
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401  (world is a fixture)


async def _seed(sp, row) -> None:
    await sp.get_storage(type(row)).create(row)


@pytest.mark.asyncio
async def test_the_current_default_agent_cannot_be_deleted_by_the_tool(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    await sp.set_default_agent_id("ag-1")

    is_error, answer = await _call(toolset, "delete_agent", id="ag-1")

    assert is_error and answer["type"] == "conflict", answer
    assert answer["message"] == default_agent_detail("ag-1"), "the tool must say what the REST route says"
    assert await sp.get_storage(Agent).get("ag-1") is not None


@pytest.mark.asyncio
async def test_the_seeded_operator_stays_deletable_for_the_tool(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row(RESERVED_OPERATOR_AGENT))
    await sp.set_default_agent_id(RESERVED_OPERATOR_AGENT)

    is_error, answer = await _call(toolset, "delete_agent", id=RESERVED_OPERATOR_AGENT)

    assert not is_error, answer
    assert await sp.get_storage(Agent).get(RESERVED_OPERATOR_AGENT) is None


@pytest.mark.asyncio
async def test_an_agent_that_is_not_the_default_is_deleted_as_before_by_the_tool(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    await _seed(sp, agent_row("ag-2"))
    await sp.set_default_agent_id("ag-2")

    is_error, answer = await _call(toolset, "delete_agent", id="ag-1")

    assert not is_error, answer


@pytest.mark.asyncio
async def test_a_graph_that_names_the_default_is_reported_before_the_default_itself(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    await _seed(sp, graph_naming_agent("g-1", "ag-1"))
    await sp.set_default_agent_id("ag-1")

    _, answer = await _call(toolset, "delete_agent", id="ag-1")
    assert "1 graph(s) reference 'ag-1'" in answer["message"]
    await _call(toolset, "delete_graph", id="g-1")
    _, answer = await _call(toolset, "delete_agent", id="ag-1")
    assert answer["message"] == default_agent_detail("ag-1")


@pytest.mark.asyncio
async def test_the_delete_agent_descriptor_names_the_default_agent_refusal(world) -> None:
    _, toolset, _ = world
    tools = {tool.id: tool async for tool in toolset.list_tools()}

    description = tools["delete_agent"].description

    assert "default agent" in description and "operator" in description, description
    # "POST /v1/setup/seed" is already in the note for the operator's re-create, so require the sentence that ties it to the default.
    assert "resets the default to" in description and "POST /v1/setup/seed" in description, description
    assert "point the default at another agent" not in description, "nothing offers that step"
    assert "default agent" not in tools["delete_graph"].description
