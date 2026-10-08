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
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from tests._support.compute_rows import (
    AGENT_BLOCKERS,
    GRAPH_BLOCKERS,
    LIVE_STATUSES,
    agent_row,
    graph_naming_agent,
    graph_naming_graph,
    graph_row,
    insert_unreadable_graph,
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


# ---- the descriptors tell an agent before it acts -------------------------------------------------------------------------------


async def _description(toolset, tool_id: str) -> str:
    descriptions = {tool.id: tool.description async for tool in toolset.list_tools()}
    return descriptions[tool_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_id,blockers",
    [
        ("delete_agent", ("agent node", "session that is not ended", "trigger subscription")),
        ("delete_graph", ("sub-graph node", "session that is not ended", "trigger subscription")),
    ],
)
async def test_the_delete_descriptor_names_the_refusal_and_what_blocks(world, tool_id: str, blockers: tuple[str, ...]) -> None:
    _, toolset, _ = world

    text = await _description(toolset, tool_id)

    assert "in_use_by" in text and "type=conflict" in text, "the descriptor must say the delete can be refused"
    for blocker in blockers:
        assert blocker in text, f"{tool_id}: the descriptor does not name {blocker!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", ["delete_collection", "delete_toolset", "update_agent", "update_graph", "get_agent"])
async def test_other_tools_do_not_claim_that_refusal(world, tool_id: str) -> None:
    _, toolset, _ = world

    assert "session that is not ended" not in await _description(toolset, tool_id)


@pytest.mark.asyncio
async def test_when_everything_blocks_the_tool_names_a_graph_first_then_a_session_then_a_subscription(world) -> None:
    """The same declared order as the REST route (``primer.storage.references``): graph, session, trigger subscription."""
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    await _seed(sp, subscription_for_agent("sub-1", "ag-1"))
    await _seed(sp, session_bound_to_agent("s-1", "ag-1", SessionStatus.WAITING))
    await _seed(sp, graph_naming_agent("g-1", "ag-1"))

    _, answer = await _call(toolset, "delete_agent", id="ag-1")
    assert "1 graph(s) reference 'ag-1' (first: 'g-1')" in answer["message"]
    await _call(toolset, "delete_graph", id="g-1")
    _, answer = await _call(toolset, "delete_agent", id="ag-1")
    assert "1 session(s) reference 'ag-1' (first: 's-1')" in answer["message"]
    await sp.get_storage(WorkspaceSession).delete("s-1")
    _, answer = await _call(toolset, "delete_agent", id="ag-1")
    assert "1 trigger subscription(s) reference 'ag-1' (first: 'sub-1')" in answer["message"]


@pytest.mark.asyncio
async def test_a_disabled_subscription_still_blocks_the_agent_for_the_tool(world) -> None:
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    await _seed(sp, subscription_for_agent("sub-1", "ag-1").model_copy(update={"enabled": False}))

    is_error, answer = await _call(toolset, "delete_agent", id="ag-1")

    assert is_error and answer["type"] == "conflict", answer
    assert await sp.get_storage(Agent).get("ag-1") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id,kind,parent", [("delete_agent", Agent, "ag-1"), ("delete_graph", Graph, "g-2")])
async def test_a_graph_row_that_cannot_be_read_blocks_the_tool_instead_of_failing_it(world, tool_id: str, kind, parent: str) -> None:
    """On the real SQLite store: the row is in the table and reading the page that holds it raises. The delete is refused as a
    ``conflict`` naming the readable graph before it, not a validation error that stops every delete."""
    sp, toolset, _ = world
    await _seed(sp, agent_row("ag-1"))
    await _seed(sp, graph_row("g-1"))
    await _seed(sp, graph_row("g-2"))
    await insert_unreadable_graph(sp, "g-9")

    is_error, answer = await _call(toolset, tool_id, id=parent)

    assert is_error and answer["type"] == "conflict", answer
    assert "unreadable graph row after g-2" in answer["message"], answer
    assert await sp.get_storage(kind).get(parent) is not None, "the row was deleted anyway"
