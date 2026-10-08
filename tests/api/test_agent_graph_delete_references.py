"""Deleting an agent or a graph that something still uses is refused with 409 ``in_use_by`` (finding A-09 of the 2026-10-08 review).

``DELETE /v1/agents/{id}`` answered 204 although a graph node, a live session and a trigger subscription named the agent: the graph's
``/status`` then reported "references missing Agent", the session stayed bound to a deleted agent and failed at its next turn, and a
trigger's next fire failed, none of them hinting at the cause at the point of deletion. Only four routers declared reference blocks.

The blockers, in the order they are looked for (the first one found is named in the answer):

* an AGENT is named by a graph's agent node, by a session that is not ended and is bound to it, and by a trigger subscription;
* a GRAPH is named by another graph's sub-graph node, by a session that is not ended and is bound to it, and by a trigger subscription.

An ENDED session is history, not a user: it never blocks. A graph naming itself is not a blocker for its own delete.
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
    session_bound_to_agent,
    session_bound_to_graph,
    subscription_for_agent,
    subscription_for_graph,
)

# Convention: shared API test fixtures (see test_compute.py).
from tests.api.conftest import app, client, fake_provider_registry  # noqa: F401


async def _seed(app, row) -> None:
    await app.state.storage_provider.get_storage(type(row)).create(row)


async def _delete(client, kind: str, entity_id: str):
    return await client.delete(f"/v1/{kind}/{entity_id}")


# ---- an agent -------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", sorted(AGENT_BLOCKERS))
async def test_an_agent_something_still_uses_cannot_be_deleted(client, app, blocker: str) -> None:
    build, detail = AGENT_BLOCKERS[blocker]
    await _seed(app, agent_row("ag-1"))
    await _seed(app, build())

    r = await _delete(client, "agents", "ag-1")

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == detail
    assert await app.state.storage_provider.get_storage(Agent).get("ag-1") is not None, "the agent was deleted anyway"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", LIVE_STATUSES, ids=lambda s: s.value)
async def test_a_session_in_any_status_but_ended_blocks_the_agent(client, app, status: SessionStatus) -> None:
    await _seed(app, agent_row("ag-1"))
    await _seed(app, session_bound_to_agent("s-1", "ag-1", status))

    r = await _delete(client, "agents", "ag-1")

    assert r.status_code == 409, f"a {status.value!r} session is live and must block: {r.text}"


@pytest.mark.asyncio
async def test_an_ended_session_does_not_block_the_agent(client, app) -> None:
    await _seed(app, agent_row("ag-1"))
    await _seed(app, session_bound_to_agent("s-1", "ag-1", SessionStatus.ENDED))

    r = await _delete(client, "agents", "ag-1")

    assert r.status_code == 204, r.text
    assert await app.state.storage_provider.get_storage(Agent).get("ag-1") is None


@pytest.mark.asyncio
async def test_rows_that_name_a_different_agent_do_not_block(client, app) -> None:
    await _seed(app, agent_row("ag-1"))
    await _seed(app, graph_naming_agent("g-other", "ag-2"))
    await _seed(app, session_bound_to_agent("s-other", "ag-2", SessionStatus.WAITING))
    await _seed(app, subscription_for_agent("sub-other", "ag-2"))
    await _seed(app, graph_row("g-empty"))
    await _seed(app, session_bound_to_graph("s-graph", "ag-1", SessionStatus.WAITING))   # a GRAPH named like the agent is not the agent

    r = await _delete(client, "agents", "ag-1")

    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_an_unreferenced_agent_is_deleted(client, app) -> None:
    await _seed(app, agent_row("ag-1"))

    r = await _delete(client, "agents", "ag-1")

    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_a_graph_past_the_first_page_still_blocks_the_agent(client, app) -> None:
    """The graph lookup walks every graph: the one that names the agent is the 205th."""
    await _seed(app, agent_row("ag-1"))
    for index in range(204):
        await _seed(app, graph_row(f"g-{index:03d}"))
    await _seed(app, graph_naming_agent("g-last", "ag-1"))

    r = await _delete(client, "agents", "ag-1")

    assert r.status_code == 409, r.text
    assert "(first: 'g-last')" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_graph_node_that_is_not_an_agent_node_never_matches(client, app) -> None:
    """A sub-graph node whose ``graph_id`` equals an agent id is not a reference to that agent."""
    await _seed(app, agent_row("shared-id"))
    await _seed(app, graph_naming_graph("g-1", "shared-id"))

    r = await _delete(client, "agents", "shared-id")

    assert r.status_code == 204, r.text


# ---- a graph --------------------------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", sorted(GRAPH_BLOCKERS))
async def test_a_graph_something_still_uses_cannot_be_deleted(client, app, blocker: str) -> None:
    build, detail = GRAPH_BLOCKERS[blocker]
    await _seed(app, graph_row("g-1"))
    await _seed(app, build())

    r = await _delete(client, "graphs", "g-1")

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == detail
    assert await app.state.storage_provider.get_storage(Graph).get("g-1") is not None, "the graph was deleted anyway"


@pytest.mark.asyncio
async def test_an_ended_session_does_not_block_the_graph(client, app) -> None:
    await _seed(app, graph_row("g-1"))
    await _seed(app, session_bound_to_graph("s-1", "g-1", SessionStatus.ENDED))

    r = await _delete(client, "graphs", "g-1")

    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_a_graph_that_names_itself_does_not_block_its_own_delete(client, app) -> None:
    await _seed(app, graph_naming_graph("g-1", "g-1"))

    r = await _delete(client, "graphs", "g-1")

    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_rows_that_name_a_different_graph_do_not_block(client, app) -> None:
    await _seed(app, graph_row("g-1"))
    await _seed(app, graph_naming_graph("g-other", "g-2"))
    await _seed(app, session_bound_to_graph("s-other", "g-2", SessionStatus.WAITING))
    await _seed(app, subscription_for_graph("sub-other", "g-2"))
    await _seed(app, session_bound_to_agent("s-agent", "g-1", SessionStatus.WAITING))     # an AGENT named like the graph is not the graph

    r = await _delete(client, "graphs", "g-1")

    assert r.status_code == 204, r.text


# ---- what the existing blocks and the not-yet-existing row still do ------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_an_agent_that_does_not_exist_is_still_a_404(client) -> None:
    assert (await _delete(client, "agents", "ghost")).status_code == 404


@pytest.mark.asyncio
async def test_the_blocking_row_is_not_touched_by_the_refusal(client, app) -> None:
    await _seed(app, agent_row("ag-1"))
    await _seed(app, session_bound_to_agent("s-1", "ag-1", SessionStatus.WAITING))

    await _delete(client, "agents", "ag-1")

    session = await app.state.storage_provider.get_storage(WorkspaceSession).get("s-1")
    assert session is not None and session.binding.agent_id == "ag-1" and session.status == SessionStatus.WAITING
