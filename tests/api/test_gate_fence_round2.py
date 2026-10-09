"""The gate fence, round 2 (console review C-033, ticket 01a11f52-9d98): the order of its checks and the places it reaches.

* A stale card is refused (409 ``approval_stale``) BEFORE the approver check (403), and a token that names the pending gate does NOT bypass the
  approver check: on ``tool_approval/respond`` and on the cancel route, which classifies an approval cancel as a rejection. A refused decision is
  not counted as a ``matched`` one.
* The cancel route takes the optional ``expected_tool_name`` of the yield the card was drawn from: a yield that is not a human gate (sleep,
  watch_files, an external wait) has no gate id, so a Skip left open for one must not cancel a DIFFERENT kind of yield that reused the raw id.
* The graph ``ask_user`` branches of the respond route (an agent node's ``pending_agent_yields`` entry, and a tool_call node's
  ``pending_toolcalls`` entry behind ``pending_dispatch``) judge the token against their own entry, as the approval branch does.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests.api.conftest import raw_client as auth_client  # noqa: F401  (auth enforced; `app` and `client` come from the conftest)
from tests.api.test_approver_routing import _login_user, _register_admin
from tests.api.test_gate_fence import G1, G2, _approval_session, _ask_user_session, _count, _fresh_metrics  # noqa: F401
from tests.api.test_gate_fence_cancel import _Published, _url


def _restricted(row: WorkspaceSession) -> WorkspaceSession:
    """The same approval, admitting only ``alice``."""
    row.parked_state["yielded"]["resume_metadata"]["approvers"] = {"kind": "users", "users": ["alice"]}
    return row


def _yield_session(*, session_id: str, tool_name: str, tool_call_id: str = "call_0") -> WorkspaceSession:
    """A park on a yield that is not a human gate (sleep, watch_files, an external wait): no gate id."""
    key = f"{tool_name}:{session_id}:{tool_call_id}"
    return WorkspaceSession(
        id=session_id, workspace_id="ws-g", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), parked_status="parked", parked_at=datetime.now(UTC), parked_event_key=key,
        parked_state={"tool_call_id": tool_call_id, "yielded": {"tool_name": tool_name, "event_key": key, "resume_metadata": {}}},
    )


# ---- the order of the checks: 409 before 403, and a token is not a bypass --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_respond_is_409_before_the_approver_check(auth_client, app):
    await _register_admin(auth_client)
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _restricted(_approval_session(session_id="o-stale", tool_call_id="call_0", gate_id=G2)))
    await _login_user(auth_client, app, "bob")                # bob is not alice

    resp = await auth_client.post("/v1/sessions/o-stale/tool_approval/respond",
                                  json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["extensions"]["code"] == "approval_stale"


@pytest.mark.asyncio
async def test_a_matching_token_does_not_bypass_the_approver_check_and_is_not_counted_as_matched(auth_client, app):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await _register_admin(auth_client)
    await storage.create(_restricted(_approval_session(session_id="o-403", tool_call_id="call_0", gate_id=G1)))
    await _login_user(auth_client, app, "bob")

    resp = await auth_client.post("/v1/sessions/o-403/tool_approval/respond",
                                  json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})

    assert resp.status_code == 403, resp.text
    assert (await storage.get("o-403")).parked_status == "parked"
    assert _count("approval", "matched") == 0, "a decision the approver check refused was not a decision"


@pytest.mark.asyncio
async def test_a_stale_cancel_is_409_before_the_approver_check(auth_client, app):
    await _register_admin(auth_client)
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _restricted(_approval_session(session_id="o-c-stale", tool_call_id="call_0", gate_id=G2)))
    await _login_user(auth_client, app, "bob")
    published = _Published(app.state.event_bus)

    resp = await auth_client.post(_url("o-c-stale", "call_0"), json={"gate_id": G1})

    assert resp.status_code == 409, resp.text
    assert resp.json()["extensions"]["code"] == "approval_stale"
    assert published.events == []


@pytest.mark.asyncio
async def test_a_matching_token_does_not_bypass_the_approver_check_on_the_cancel_route(auth_client, app):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await _register_admin(auth_client)
    await storage.create(_restricted(_approval_session(session_id="o-c-403", tool_call_id="call_0", gate_id=G1)))
    await _login_user(auth_client, app, "bob")
    published = _Published(app.state.event_bus)

    resp = await auth_client.post(_url("o-c-403", "call_0"), json={"gate_id": G1})

    assert resp.status_code == 403, resp.text
    assert published.events == [], "cancelling an approval IS rejecting it: nothing may be published for a user the gate does not admit"
    assert (await storage.get("o-c-403")).parked_status == "parked"
    assert _count("approval", "matched") == 0


# ---- expected_tool_name: a Skip for one kind of yield cannot cancel another ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["sleep", "watch_files", "_external"])
async def test_a_cancel_expecting_a_non_gate_yield_is_refused_when_the_park_is_a_different_kind(app, client, tool_name):
    """Round 1 parked on a sleep ("call_0") whose Skip stayed open; round 3 parked an ask_user under "call_0". The Skip must not answer the question."""
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _ask_user_session(session_id="k-mismatch", tool_call_id="call_0", gate_id=G2))
    published = _Published(app.state.event_bus)

    resp = await client.post(_url("k-mismatch", "call_0"), json={"expected_tool_name": tool_name})

    assert resp.status_code == 409, resp.text
    assert resp.json()["extensions"]["code"] == "approval_stale"
    assert published.events == []
    assert (await app.state.storage_provider.get_storage(WorkspaceSession).get("k-mismatch")).parked_status == "parked"


@pytest.mark.asyncio
async def test_a_cancel_expecting_an_ask_user_is_refused_when_the_park_is_a_sleep(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_yield_session(session_id="k-sleep", tool_name="sleep"))
    published = _Published(app.state.event_bus)

    resp = await client.post(_url("k-sleep", "call_0"), json={"expected_tool_name": "ask_user"})

    assert resp.status_code == 409, resp.text
    assert published.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["sleep", "watch_files", "_external"])
async def test_a_cancel_expecting_the_kind_that_is_parked_is_accepted(app, client, tool_name):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_yield_session(session_id="k-ok", tool_name=tool_name))
    published = _Published(app.state.event_bus)

    resp = await client.post(_url("k-ok", "call_0"), json={"expected_tool_name": tool_name})

    assert resp.status_code == 202, resp.text
    assert [k for k, _ in published.events] == [f"{tool_name}:k-ok:call_0"]


@pytest.mark.asyncio
async def test_a_cancel_expecting_an_approval_is_judged_by_the_gate_kind_not_the_graph_label(app, client):
    from tests.api.test_gate_fence import _graph_two_gate_session

    await app.state.storage_provider.get_storage(WorkspaceSession).create(_graph_two_gate_session(session_id="k-graph"))
    published = _Published(app.state.event_bus)

    refused = await client.post(_url("k-graph", "call-0"), json={"expected_tool_name": "ask_user", "gate_id": G1})
    assert refused.status_code == 409, refused.text
    assert published.events == []

    ok = await client.post(_url("k-graph", "call-0"), json={"expected_tool_name": "_approval", "gate_id": G1})
    assert ok.status_code == 202, ok.text


@pytest.mark.asyncio
async def test_a_cancel_that_names_no_expected_tool_is_accepted_as_before(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_yield_session(session_id="k-none", tool_name="sleep"))

    resp = await client.post(_url("k-none", "call_0"), json={})

    assert resp.status_code == 202, resp.text


@pytest.mark.asyncio
async def test_the_refusal_of_a_yield_that_is_not_a_gate_does_not_call_it_an_approval(app, client):
    """The 409 for a non-gate says the YIELD was replaced, not 'this request' or 'this approval'."""
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_yield_session(session_id="k-words", tool_name="sleep"))

    resp = await client.post(_url("k-words", "call_0"), json={"expected_tool_name": "watch_files"})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "yield" in detail and "approval" not in detail and "question" not in detail, detail


# ---- the graph ask_user branches of the respond route --------------------------------------------------------------------------------------------


def _graph_ask_user_session(*, session_id: str, via: str) -> WorkspaceSession:
    """Two ask_user prompts of one superstep that share the raw id "dup": an AGENT node's (``pending_agent_yields``) or a TOOL_CALL node's
    (``pending_toolcalls`` behind ``pending_dispatch``)."""
    nodes = (("n0", G1), ("n1", G2))
    keys = [f"ask_user:{session_id}:{node}:dup" for node, _ in nodes]
    checkpoint: dict = {"pending_toolcalls": [], "pending_agent_yields": [], "pending_dispatch": []}
    for (node, gid), key in zip(nodes, keys, strict=True):
        meta = {"prompt": f"question of {node}", "response_schema": None, "gate_id": gid}
        if via == "agent":
            checkpoint["pending_agent_yields"].append(
                {"node_id": node, "tool_call_id": "dup", "tool_name": "ask_user", "event_key": key, "resume_metadata": meta})
        else:
            checkpoint["pending_toolcalls"].append(
                {"node_id": node, "tool_call_id": "dup", "tool_name": "ask_user", "parked_event_key": key, "arguments": {},
                 "resume_metadata": meta, "scoped_tool_call_id": None})
            checkpoint["pending_dispatch"].append({"kind": "ask_user", "node_id": node, "tool_call_id": "dup", "resume_metadata": meta})
    return WorkspaceSession(
        id=session_id, workspace_id="ws-g", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), parked_status="parked", parked_at=datetime.now(UTC), parked_event_key=keys[0], parked_event_keys=keys,
        parked_state={
            "tool_call_id": "dup",
            "yielded": {"tool_name": "_approval", "event_key": keys[0], "resume_metadata": {"gate_id": G1}, "event_keys": keys},
            "graph_checkpoint": checkpoint,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["agent", "tool_call"])
async def test_a_graph_ask_user_respond_naming_the_second_gate_answers_that_gate(app, client, via):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_graph_ask_user_session(session_id="g-ask", via=via))
    published = _Published(app.state.event_bus)

    resp = await client.post("/v1/sessions/g-ask/ask_user/respond", json={"tool_call_id": "dup", "gate_id": G2, "response": "blue"})

    assert resp.status_code == 202, resp.text
    assert [k for k, _ in published.events] == ["ask_user:g-ask:n1:dup"], "the sibling that shares the raw id must not be the one answered"
    assert _count("ask_user", "matched") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["agent", "tool_call"])
async def test_a_graph_ask_user_respond_naming_a_replaced_gate_is_409_and_moves_nothing(app, client, via):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_graph_ask_user_session(session_id="g-ask-stale", via=via))
    published = _Published(app.state.event_bus)

    resp = await client.post("/v1/sessions/g-ask-stale/ask_user/respond",
                             json={"tool_call_id": "dup", "gate_id": "c" * 32, "response": "blue"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["extensions"]["code"] == "approval_stale"
    assert published.events == []
    assert _count("ask_user", "stale") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["agent", "tool_call"])
async def test_a_graph_ask_user_respond_naming_no_gate_is_accepted_and_counted(app, client, via):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_graph_ask_user_session(session_id="g-ask-bare", via=via))
    published = _Published(app.state.event_bus)

    resp = await client.post("/v1/sessions/g-ask-bare/ask_user/respond", json={"tool_call_id": "dup", "response": "blue"})

    assert resp.status_code == 202, resp.text
    assert [k for k, _ in published.events] == ["ask_user:g-ask-bare:n0:dup"]
    assert _count("ask_user", "absent") == 1
