"""The system ``delete_agent`` and ``delete_graph`` tools refuse what the REST routes refuse (finding A-09, through the D5 guards).

The REST routers (``tests/api/test_agent_graph_delete_references.py``) refuse to delete an agent or a graph that a graph, a live
session or a trigger subscription still names. The system toolset's generic delete tools re-implement the verb, so an agent could
delete what the route refuses. This runs the SAME blockers (one table in ``tests/_support/compute_rows.py``) through the tools on a real
SQLite store, and requires the tool's message to be the REST detail word for word, so the two surfaces cannot drift on what blocks
or on what they say.
"""

from __future__ import annotations

import pytest

from primer.model.agent import Agent
from primer.model.graph import Graph
from primer.model.workspace_session import SessionStatus
from tests._support.compute_rows import (
    AGENT_BLOCKERS,
    GRAPH_BLOCKERS,
    LIVE_STATUSES,
    agent_row,
    graph_naming_agent,
    graph_naming_graph,
    graph_row,
    session_bound_to_agent,
    session_bound_to_graph,
    subscription_for_agent,
    subscription_for_graph,
)
from tests.toolset.test_system_crud_guards import _call, world  # noqa: F401  (world is a fixture)


async def _seed(sp, row) -> None:
    await sp.get_storage(type(row)).create(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", sorted(AGENT_BLOCKERS))
async def test_an_agent_something_still_uses_cannot_be_deleted_by_the_tool(world, blocker: str) -> None:
    sp, toolset, _ = world
    build, detail = AGENT_BLOCKERS[blocker]
    await _seed(sp, agent_row("ag-1"))
    await _seed(sp, build())

    is_error, answer = await _call(toolset, "delete_agent", id="ag-1")

    assert is_error and answer["type"] == "conflict", answer
    assert answer["message"] == detail, "the tool must say what the REST route says"
    assert await sp.get_storage(Agent).get("ag-1") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LIVE_STATUSES, ids=lambda s: s.value)
async def test_a_session_in_any_status_but_ended_blocks_the_agent_for_the_tool(world, status: SessionStatus) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    await _seed(sp, session_bound_to_agent("s-1", "ag-1", status))

    is_error, answer = await _call(toolset, "delete_agent", id="ag-1")

    assert is_error and answer["type"] == "conflict", f"a {status.value!r} session is live and must block: {answer}"


@pytest.mark.asyncio
async def test_an_ended_session_does_not_block_the_agent_for_the_tool(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    await _seed(sp, session_bound_to_agent("s-1", "ag-1", SessionStatus.ENDED))

    is_error, _ = await _call(toolset, "delete_agent", id="ag-1")

    assert not is_error
    assert await sp.get_storage(Agent).get("ag-1") is None


@pytest.mark.asyncio
async def test_rows_that_name_a_different_agent_do_not_block_the_tool(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    await _seed(sp, graph_naming_agent("g-other", "ag-2"))
    await _seed(sp, session_bound_to_agent("s-other", "ag-2", SessionStatus.WAITING))
    await _seed(sp, subscription_for_agent("sub-other", "ag-2"))
    await _seed(sp, session_bound_to_graph("s-graph", "ag-1", SessionStatus.WAITING))

    is_error, _ = await _call(toolset, "delete_agent", id="ag-1")

    assert not is_error


@pytest.mark.asyncio
async def test_a_graph_past_the_first_page_still_blocks_the_agent_for_the_tool(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    for index in range(204):
        await _seed(sp, graph_row(f"g-{index:03d}"))
    await _seed(sp, graph_naming_agent("g-last", "ag-1"))

    is_error, answer = await _call(toolset, "delete_agent", id="ag-1")

    assert is_error and "(first: 'g-last')" in answer["message"]


@pytest.mark.asyncio
async def test_a_sub_graph_node_named_like_an_agent_is_not_a_reference_to_the_agent(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row("shared-id"))
    await _seed(sp, graph_naming_graph("g-1", "shared-id"))

    is_error, _ = await _call(toolset, "delete_agent", id="shared-id")

    assert not is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", sorted(GRAPH_BLOCKERS))
async def test_a_graph_something_still_uses_cannot_be_deleted_by_the_tool(world, blocker: str) -> None:
    sp, toolset, _ = world
    build, detail = GRAPH_BLOCKERS[blocker]
    await _seed(sp, graph_row("g-1"))
    await _seed(sp, build())

    is_error, answer = await _call(toolset, "delete_graph", id="g-1")

    assert is_error and answer["type"] == "conflict", answer
    assert answer["message"] == detail, "the tool must say what the REST route says"
    assert await sp.get_storage(Graph).get("g-1") is not None


@pytest.mark.asyncio
async def test_an_ended_session_and_a_self_reference_do_not_block_the_graph_for_the_tool(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, graph_naming_graph("g-1", "g-1"))
    await _seed(sp, session_bound_to_graph("s-1", "g-1", SessionStatus.ENDED))

    is_error, _ = await _call(toolset, "delete_graph", id="g-1")

    assert not is_error
