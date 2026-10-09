"""The audit record the respond route writes is keyed by the gate it decided (console review C-033 PR 2, ticket 01a11f52-9d98).

The key is derived from the PARK (its ``gate_id``), not from what the request said, so a respond with the token, one without it and the resume-time
fallback all land on the same key for one gate, and a later gate under the same raw tool_call_id gets another (see
``tests/agent/test_approval_record_key.py`` for the dedupe against a real unique index; the API test double does not enforce one).
"""

from __future__ import annotations

import pytest

from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import WorkspaceSession
from tests.api.test_gate_fence import G1, G2, _approval_session, _graph_two_gate_session


async def _records(app, session_id: str) -> list[ToolApprovalRecord]:
    page = await app.state.storage_provider.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=50))
    return [r for r in page.items if r.session_id == session_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("body_extra", [{"gate_id": G1}, {}], ids=["with-token", "tokenless"])
async def test_the_record_is_keyed_by_the_gate_the_park_carries(app, client, body_extra):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _approval_session(session_id="rk-one", tool_call_id="call_0", gate_id=G1))

    resp = await client.post("/v1/sessions/rk-one/tool_approval/respond", json={"tool_call_id": "call_0", "decision": "approved", **body_extra})

    assert resp.status_code == 202, resp.text
    assert [r.gate_event_key for r in await _records(app, "rk-one")] == [f"tool_approval:rk-one:call_0@{G1}"]


@pytest.mark.asyncio
async def test_a_park_from_before_gates_had_ids_is_keyed_by_the_event_key_as_before(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _approval_session(session_id="rk-legacy", tool_call_id="call_0", gate_id=None))

    await client.post("/v1/sessions/rk-legacy/tool_approval/respond", json={"tool_call_id": "call_0", "decision": "approved"})

    assert [r.gate_event_key for r in await _records(app, "rk-legacy")] == ["tool_approval:rk-legacy:call_0"]


@pytest.mark.asyncio
async def test_each_gate_of_a_multi_gate_park_is_keyed_by_its_own_id(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_graph_two_gate_session(session_id="rk-multi"))

    resp = await client.post("/v1/sessions/rk-multi/tool_approval/respond", json={"tool_call_id": "call-1", "gate_id": G2, "decision": "approved"})

    assert resp.status_code == 202, resp.text
    assert [r.gate_event_key for r in await _records(app, "rk-multi")] == [f"tool_approval:rk-multi:call-1@{G2}"]


@pytest.mark.asyncio
async def test_the_records_list_finds_a_record_by_its_gate_key(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _approval_session(session_id="rk-find", tool_call_id="call_0", gate_id=G1))
    await client.post("/v1/sessions/rk-find/tool_approval/respond", json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})

    found = await client.get("/v1/tool_approval/records", params={"gate_event_key": f"tool_approval:rk-find:call_0@{G1}"})
    missed = await client.get("/v1/tool_approval/records", params={"gate_event_key": "tool_approval:rk-find:call_0"})

    assert found.status_code == 200 and [r["tool_call_id"] for r in found.json()["items"]] == ["call_0"]
    assert missed.status_code == 200 and missed.json()["items"] == []


@pytest.mark.asyncio
async def test_the_records_filter_says_it_matches_the_stored_key_suffix_included(app, client):
    """The OpenAPI text of ``?gate_event_key=`` names the stored form, so a caller that has the bare event key in hand knows it will not match a gated record."""
    schema = (await client.get("/openapi.json")).json()
    params = schema["paths"]["/v1/tool_approval/records"]["get"]["parameters"]
    text = next(p for p in params if p["name"] == "gate_event_key")["description"]

    assert "<event_key>@<gate_id>" in text and "matched exactly" in text and "bare event key" in text, text


@pytest.mark.asyncio
async def test_the_bare_event_key_does_not_find_a_gated_record(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _approval_session(session_id="rk-filter", tool_call_id="call_0", gate_id=G1))
    await client.post("/v1/sessions/rk-filter/tool_approval/respond", json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})

    bare = await client.get("/v1/tool_approval/records", params={"gate_event_key": "tool_approval:rk-filter:call_0"})
    stored = await client.get("/v1/tool_approval/records", params={"gate_event_key": f"tool_approval:rk-filter:call_0@{G1}"})

    assert bare.json()["items"] == []
    assert [r["gate_event_key"] for r in stored.json()["items"]] == [f"tool_approval:rk-filter:call_0@{G1}"]
