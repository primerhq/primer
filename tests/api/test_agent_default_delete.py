"""The system default agent cannot be deleted while it is the default (A-09 follow-up; the lead's ruling on ticket 01a11a8b).

``SystemState.default_agent_id`` names the agent a session created WITHOUT a binding runs. Deleting that agent still answered 204, and every
binding-less session create then failed at its first turn. The delete is now refused with 409 ``in_use_by`` while the agent is the default,
with ONE exception: the seeded ``operator``, which is the default on every install and stays deletable (ADM-13: setup reopens when it is
gone and the seed re-creates it). The setting is read through the storage provider's ``get_system_state`` (it is a row of the ``system_state``
table, not a ``Storage`` entity, so it is not one of ``AGENT_REFERENCES``).

Note on reachability: no route or tool sets the default; the seed pass stamps it to the operator, so another agent can be the default only if
it was written to the database directly. The refusal is for that state, and its message says what to do: the only reset is the seed pass
(``POST /v1/setup/seed``, or a server restart), which stamps the default back to the operator. It used to say "point the default agent at
another agent", which nothing can do.
"""

from __future__ import annotations

import pytest

from primer.bootstrap.defaults import RESERVED_OPERATOR_AGENT
from primer.model.agent import Agent
from primer.model.workspace_session import SessionStatus
from tests._support.compute_rows import (
    agent_row,
    default_agent_detail,
    graph_naming_agent,
    session_bound_to_agent,
)

# Convention: shared API test fixtures (see test_compute.py).
from tests.api.conftest import app, client, fake_provider_registry  # noqa: F401


async def _seed(app, row) -> None:
    await app.state.storage_provider.get_storage(type(row)).create(row)


@pytest.mark.asyncio
async def test_the_current_default_agent_cannot_be_deleted(client, app) -> None:
    await _seed(app, agent_row("ag-1"))
    await app.state.storage_provider.set_default_agent_id("ag-1")

    r = await client.delete("/v1/agents/ag-1")

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == default_agent_detail("ag-1")
    assert await app.state.storage_provider.get_storage(Agent).get("ag-1") is not None, "the default agent was deleted anyway"


@pytest.mark.asyncio
async def test_the_remedy_the_refusal_names_is_one_that_works(client, app) -> None:
    """The 409 names ``POST /v1/setup/seed`` (and a restart); running it must really make the agent deletable. A sentence that sends the
    caller to a step that does nothing is the defect this pins."""
    await _seed(app, agent_row("ag-1"))
    await app.state.storage_provider.set_default_agent_id("ag-1")
    refused = await client.delete("/v1/agents/ag-1")
    assert refused.status_code == 409, refused.text
    assert "POST /v1/setup/seed" in refused.json()["detail"], refused.json()["detail"]
    assert "restart the server" in refused.json()["detail"], refused.json()["detail"]
    assert "another agent" not in refused.json()["detail"], "the old advice names a step no route or tool offers"

    assert (await client.post("/v1/setup/seed")).status_code == 200
    assert (await app.state.storage_provider.get_system_state()).default_agent_id == RESERVED_OPERATOR_AGENT

    assert (await client.delete("/v1/agents/ag-1")).status_code == 204


@pytest.mark.asyncio
async def test_the_seeded_operator_stays_deletable_even_when_it_is_the_default(client, app) -> None:
    # The app fixture seeds the operator at startup, as a real install does; only make sure it is there.
    if await app.state.storage_provider.get_storage(Agent).get(RESERVED_OPERATOR_AGENT) is None:
        await _seed(app, agent_row(RESERVED_OPERATOR_AGENT))
    await app.state.storage_provider.set_default_agent_id(RESERVED_OPERATOR_AGENT)

    r = await client.delete(f"/v1/agents/{RESERVED_OPERATOR_AGENT}")

    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_an_agent_that_is_not_the_default_is_deleted_as_before(client, app) -> None:
    await _seed(app, agent_row("ag-1"))
    await _seed(app, agent_row("ag-2"))
    await app.state.storage_provider.set_default_agent_id("ag-2")

    r = await client.delete("/v1/agents/ag-1")

    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_with_no_default_set_every_agent_is_deletable(client, app) -> None:
    await _seed(app, agent_row("ag-1"))

    assert (await client.delete("/v1/agents/ag-1")).status_code == 204


@pytest.mark.asyncio
async def test_once_the_default_points_elsewhere_the_old_default_can_be_deleted(client, app) -> None:
    await _seed(app, agent_row("ag-1"))
    await app.state.storage_provider.set_default_agent_id("ag-1")
    assert (await client.delete("/v1/agents/ag-1")).status_code == 409

    await app.state.storage_provider.set_default_agent_id(RESERVED_OPERATOR_AGENT)

    assert (await client.delete("/v1/agents/ag-1")).status_code == 204


@pytest.mark.asyncio
async def test_a_graph_or_session_that_names_the_default_is_reported_first(client, app) -> None:
    """The reference blocks run before the default-agent check, each removed in turn: graph, then session, then the default itself."""
    await _seed(app, agent_row("ag-1"))
    await _seed(app, graph_naming_agent("g-1", "ag-1"))
    await _seed(app, session_bound_to_agent("s-1", "ag-1", SessionStatus.WAITING))
    await app.state.storage_provider.set_default_agent_id("ag-1")

    assert "1 graph(s) reference 'ag-1'" in (await client.delete("/v1/agents/ag-1")).json()["detail"]
    assert (await client.delete("/v1/graphs/g-1")).status_code == 204
    assert "1 session(s) reference 'ag-1'" in (await client.delete("/v1/agents/ag-1")).json()["detail"]
    await app.state.storage_provider.get_storage(type(session_bound_to_agent("x", "ag-1", SessionStatus.WAITING))).delete("s-1")
    assert (await client.delete("/v1/agents/ag-1")).json()["detail"] == default_agent_detail("ag-1")


@pytest.mark.asyncio
async def test_the_default_agent_refusal_body(client, app) -> None:
    """The whole 409 body, byte for byte as the other reference refusals (tests/api/test_agent_graph_reference_wire_bodies.py): status,
    content type and the raw problem+json with the per-request id normalised. The body did not exist before, so it was taken from the
    first green run and is the contract from here on."""
    import re

    await _seed(app, agent_row("ag-1"))
    await app.state.storage_provider.set_default_agent_id("ag-1")

    r = await client.delete("/v1/agents/ag-1")

    expected = (
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"' + default_agent_detail("ag-1")
        + '","instance":"/v1/agents/ag-1","extensions":{"request_id":"req-<id>"}}'
    )
    assert (r.status_code, r.headers["content-type"], re.sub(r'"request_id":"req-[0-9a-f]+"', '"request_id":"req-<id>"', r.text)) == (
        409, "application/problem+json", expected,
    )
