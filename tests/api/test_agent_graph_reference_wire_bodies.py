"""The 409 bodies of the agent and graph reference blocks, pinned byte for byte (finding A-09; the lead asked for wire goldens).

``tests/api/test_agent_graph_delete_references.py`` asserts the status and the ``detail`` sentence. This pins the WHOLE response of each
refusal (status, content type and the raw RFC 7807 body, key order included), so a change to the envelope, the error type or the wording a
client or the console reads shows up here. The system tools answer the same sentence (``tests/toolset/test_system_agent_graph_references.py``).

Unlike ``test_rest_validator_wire_bodies.py`` these bodies did not exist before the change (the delete used to answer 204), so there is no
before/after capture: ``GOLDEN`` was taken from the first green run of these exact requests and is the contract from here on. The one
volatile part, the per-request id, is normalised.
"""

from __future__ import annotations

import re

import pytest

from tests._support.compute_rows import AGENT_BLOCKERS, GRAPH_BLOCKERS, agent_row, graph_row
from tests.api.conftest import app, client, fake_provider_registry  # noqa: F401

GOLDEN: dict[str, tuple[int, str, str]] = {
    "agent_graph node": (
        409,
        "application/problem+json",
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"in_use_by: 1 graph(s) reference \'ag-1\' (first: \'g-1\')","instance":"/v1/agents/ag-1","extensions":{"request_id":"req-<id>"}}',
    ),
    "agent_live session": (
        409,
        "application/problem+json",
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"in_use_by: 1 session(s) reference \'ag-1\' (first: \'s-1\')","instance":"/v1/agents/ag-1","extensions":{"request_id":"req-<id>"}}',
    ),
    "agent_trigger subscription": (
        409,
        "application/problem+json",
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"in_use_by: 1 trigger subscription(s) reference \'ag-1\' (first: \'sub-1\')","instance":"/v1/agents/ag-1","extensions":{"request_id":"req-<id>"}}',
    ),
    "graph_sub-graph node": (
        409,
        "application/problem+json",
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"in_use_by: 1 graph(s) reference \'g-1\' (first: \'g-parent\')","instance":"/v1/graphs/g-1","extensions":{"request_id":"req-<id>"}}',
    ),
    "graph_live session": (
        409,
        "application/problem+json",
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"in_use_by: 1 session(s) reference \'g-1\' (first: \'s-1\')","instance":"/v1/graphs/g-1","extensions":{"request_id":"req-<id>"}}',
    ),
    "graph_trigger subscription": (
        409,
        "application/problem+json",
        '{"type":"/errors/conflict","title":"Conflict","status":409,"detail":"in_use_by: 1 trigger subscription(s) reference \'g-1\' (first: \'sub-1\')","instance":"/v1/graphs/g-1","extensions":{"request_id":"req-<id>"}}',
    ),
}

_REQUEST_ID = re.compile(r'"request_id":"req-[0-9a-f]+"')


def _normalise(text: str) -> str:
    return _REQUEST_ID.sub('"request_id":"req-<id>"', text)


def _check(name: str, response) -> None:
    actual = (response.status_code, response.headers["content-type"], _normalise(response.text))
    assert name in GOLDEN, f"no golden for {name!r}; the actual response is {actual!r}"
    assert actual == GOLDEN[name]


async def _seed(app, row) -> None:
    await app.state.storage_provider.get_storage(type(row)).create(row)


def test_every_blocker_has_a_golden_body() -> None:
    expected = {f"agent_{name}" for name in AGENT_BLOCKERS} | {f"graph_{name}" for name in GRAPH_BLOCKERS}
    assert set(GOLDEN) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", sorted(AGENT_BLOCKERS))
async def test_the_agent_delete_refusal_body(client, app, blocker: str) -> None:
    build, _ = AGENT_BLOCKERS[blocker]
    await _seed(app, agent_row("ag-1"))
    await _seed(app, build())

    _check(f"agent_{blocker}", await client.delete("/v1/agents/ag-1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", sorted(GRAPH_BLOCKERS))
async def test_the_graph_delete_refusal_body(client, app, blocker: str) -> None:
    build, _ = GRAPH_BLOCKERS[blocker]
    await _seed(app, graph_row("g-1"))
    await _seed(app, build())

    _check(f"graph_{blocker}", await client.delete("/v1/graphs/g-1"))
