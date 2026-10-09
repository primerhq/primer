"""A decision names the gate it answers, not only the raw provider tool_call_id (console review C-033, ticket 01a11f52-9d98).

A provider repeats its tool_call_id across rounds ("call_0" in round 1 and again in round 3), and the approval / ask_user event key is built
from that raw id. A card left open from round 1 therefore decided whatever gate was pending under the same id later. Every approval and
ask_user park now carries a ``gate_id`` minted when the gate is created (``resume_metadata.gate_id``); the pending responses serve it, the
respond routes take it back, and a respond naming a gate that is no longer the pending one is a 409 ``approval_stale`` that moves nothing.

A respond with no ``gate_id`` is still accepted (clients that predate the token), logged once without arguments and counted, so the flip to a
422 can be scheduled after one release. A token that is not a gate id is a 422.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession

G1 = "a" * 32
G2 = "b" * 32


def _approval_session(*, session_id: str, tool_call_id: str, gate_id: str | None, workspace_id: str = "ws-g") -> WorkspaceSession:
    ek = f"tool_approval:{session_id}:{tool_call_id}"
    metadata: dict = {
        "policy_id": "pol", "approval_type": "required", "gate_reason": "matched policy", "approvers": None,
        "original_call": {"id": tool_call_id, "name": "delete_workspace", "arguments": {"secret_arg": "do-not-log-me"}},
    }
    if gate_id is not None:
        metadata["gate_id"] = gate_id
    return WorkspaceSession(
        id=session_id, workspace_id=workspace_id, binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC), parked_status="parked", parked_at=datetime.now(UTC), parked_event_key=ek,
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {"tool_name": "_approval", "event_key": ek, "resume_metadata": metadata},
        },
    )


def _ask_user_session(*, session_id: str, tool_call_id: str, gate_id: str | None, workspace_id: str = "ws-g") -> WorkspaceSession:
    ek = f"ask_user:{session_id}:{tool_call_id}"
    metadata: dict = {"prompt": "Which currency?", "response_schema": None, "tool_call_id": tool_call_id, "files": None}
    if gate_id is not None:
        metadata["gate_id"] = gate_id
    return WorkspaceSession(
        id=session_id, workspace_id=workspace_id, binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC), parked_status="parked", parked_at=datetime.now(UTC), parked_event_key=ek,
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {"tool_name": "ask_user", "event_key": ek, "resume_metadata": metadata},
        },
    )


def _graph_two_gate_session(*, session_id: str, same_raw_id: bool = False) -> WorkspaceSession:
    """Two concurrent approval gates of one superstep, each with its own gate_id; ``same_raw_id`` gives both the same provider id."""
    ids = ("dup", "dup") if same_raw_id else ("call-0", "call-1")
    entries = []
    for i, (node, tcid, gid) in enumerate(zip(("worker[0]", "worker[1]"), ids, (G1, G2), strict=True)):
        scope = f"{node}:" if same_raw_id else ""
        entries.append({
            "node_id": node, "tool_call_id": tcid, "parked_event_key": f"tool_approval:{session_id}:{scope}{tcid}",
            "arguments": {"id": f"ws-{i}"}, "tool_name": "_approval",
            "resume_metadata": {
                "policy_id": "pol", "approval_type": "required", "gate_reason": "matched policy", "approvers": None, "gate_id": gid,
                "original_call": {"id": tcid, "name": "delete_workspace", "arguments": {"id": f"ws-{i}"}},
            },
            "scoped_tool_call_id": None,
        })
    keys = [e["parked_event_key"] for e in entries]
    primary = entries[0]
    return WorkspaceSession(
        id=session_id, workspace_id="ws-g", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), parked_status="parked", parked_at=datetime.now(UTC), parked_event_key=primary["parked_event_key"],
        parked_event_keys=keys,
        parked_state={
            "tool_call_id": primary["tool_call_id"],
            "yielded": {
                "tool_name": "_approval", "event_key": primary["parked_event_key"],
                "resume_metadata": {"original_call": primary["resume_metadata"]["original_call"], "gate_id": G1}, "event_keys": keys,
            },
            "graph_checkpoint": {"pending_toolcalls": entries, "pending_agent_yields": [], "pending_dispatch": []},
        },
    )


def _count(kind: str, token: str) -> float:
    import primer.observability.metrics as m

    return m.registry.get_sample_value("gate_respond_total", {"kind": kind, "gate_token": token}) or 0.0


async def _records(app, session_id: str) -> list[ToolApprovalRecord]:
    page = await app.state.storage_provider.get_storage(ToolApprovalRecord).list(OffsetPage(offset=0, length=50))
    return [r for r in page.items if r.session_id == session_id]


@pytest.fixture(autouse=True)
def _fresh_metrics():
    import primer.observability.metrics as m

    m.reset_for_test()
    yield
    m.reset_for_test()


# ---- approvals -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pending_approval_serves_the_gate_id(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _approval_session(session_id="f-pend", tool_call_id="call_0", gate_id=G1))

    resp = await client.get("/v1/sessions/f-pend/tool_approval/pending")

    assert resp.status_code == 200, resp.text
    assert resp.json()["gate_id"] == G1


@pytest.mark.asyncio
async def test_a_respond_naming_the_pending_gate_is_accepted(app, client):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_approval_session(session_id="f-ok", tool_call_id="call_0", gate_id=G1))

    resp = await client.post("/v1/sessions/f-ok/tool_approval/respond",
                             json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})

    assert resp.status_code == 202, resp.text
    assert (await storage.get("f-ok")).parked_status == "resumable"
    assert _count("approval", "matched") == 1


@pytest.mark.asyncio
async def test_a_stale_card_for_a_repeated_raw_id_is_409_approval_stale_and_moves_nothing(app, client):
    """Round 1 parked "call_0" under G1 and its card stayed open; round 3 parked "call_0" again under G2. The old card must not decide the new gate."""
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_approval_session(session_id="f-stale", tool_call_id="call_0", gate_id=G2))

    resp = await client.post("/v1/sessions/f-stale/tool_approval/respond",
                             json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})

    assert resp.status_code == 409, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["extensions"]["code"] == "approval_stale"
    assert "replaced" in body["detail"]
    row = await storage.get("f-stale")
    assert row.parked_status == "parked"                      # nothing moved
    assert "resume_event_payload" not in (row.parked_state or {})
    assert await _records(app, "f-stale") == []               # and nothing was recorded as a decision
    assert _count("approval", "stale") == 1


@pytest.mark.asyncio
async def test_a_token_for_a_gate_with_no_stamped_id_is_stale(app, client):
    """A park from before the token existed has no gate_id; a client that names one cannot have got it from this gate."""
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _approval_session(session_id="f-legacy", tool_call_id="call_0", gate_id=None))

    resp = await client.post("/v1/sessions/f-legacy/tool_approval/respond",
                             json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["extensions"]["code"] == "approval_stale"


@pytest.mark.asyncio
async def test_a_tokenless_respond_is_accepted_logged_without_arguments_and_counted(app, client, caplog):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_approval_session(session_id="f-bare", tool_call_id="call_0", gate_id=G1))

    with caplog.at_level(logging.INFO, logger="primer.session.gate_token"):
        resp = await client.post("/v1/sessions/f-bare/tool_approval/respond", json={"tool_call_id": "call_0", "decision": "approved"})

    assert resp.status_code == 202, resp.text
    assert (await storage.get("f-bare")).parked_status == "resumable"
    lines = [r.getMessage() for r in caplog.records if r.name == "primer.session.gate_token"]
    assert len(lines) == 1 and "f-bare" in lines[0]
    assert "do-not-log-me" not in lines[0] and "delete_workspace" not in lines[0]       # a session id, never the call's arguments
    assert _count("approval", "absent") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "not-a-gate-id", "A" * 32, "a" * 31, "a" * 33, 123, ["a" * 32]])
async def test_a_malformed_gate_id_is_422_and_moves_nothing(app, client, token):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_approval_session(session_id="f-bad", tool_call_id="call_0", gate_id=G1))

    resp = await client.post("/v1/sessions/f-bad/tool_approval/respond",
                             json={"tool_call_id": "call_0", "gate_id": token, "decision": "approved"})

    assert resp.status_code == 422, resp.text
    assert (await storage.get("f-bad")).parked_status == "parked"


@pytest.mark.asyncio
async def test_each_gate_of_a_multi_gate_park_is_fenced_by_its_own_id(app, client):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_graph_two_gate_session(session_id="f-multi"))

    crossed = await client.post("/v1/sessions/f-multi/tool_approval/respond",
                                json={"tool_call_id": "call-0", "gate_id": G2, "decision": "approved"})
    assert crossed.status_code == 409, crossed.text
    assert (await storage.get("f-multi")).parked_status == "parked"

    second = await client.post("/v1/sessions/f-multi/tool_approval/respond",
                               json={"tool_call_id": "call-1", "gate_id": G2, "decision": "approved"})
    assert second.status_code == 202, second.text
    payloads = (await storage.get("f-multi")).parked_state["resume_event_payloads"]
    assert [p["event_key"] for p in payloads.values()] == ["tool_approval:f-multi:call-1"]


@pytest.mark.asyncio
async def test_two_siblings_sharing_a_raw_id_are_told_apart_by_their_gate_ids(app, client):
    """The wire used to have no field to tell them apart: the first match won, with a warning. The gate id is that field."""
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_graph_two_gate_session(session_id="f-dup", same_raw_id=True))

    resp = await client.post("/v1/sessions/f-dup/tool_approval/respond",
                             json={"tool_call_id": "dup", "gate_id": G2, "decision": "approved"})

    assert resp.status_code == 202, resp.text
    payloads = (await storage.get("f-dup")).parked_state["resume_event_payloads"]
    assert [p["event_key"] for p in payloads.values()] == ["tool_approval:f-dup:worker[1]:dup"]
    assert [r.gate_event_key for r in await _records(app, "f-dup")] == [f"tool_approval:f-dup:worker[1]:dup@{G2}"]       # C-033 PR 2: keyed by the gate


@pytest.mark.asyncio
async def test_a_respond_for_a_call_that_is_not_pending_is_still_404_with_or_without_a_token(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _approval_session(session_id="f-404", tool_call_id="call_0", gate_id=G1))

    for extra in ({}, {"gate_id": G1}):
        resp = await client.post("/v1/sessions/f-404/tool_approval/respond",
                                 json={"tool_call_id": "call_9", "decision": "approved", **extra})
        assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_the_session_yields_lister_and_the_inbox_serve_the_gate_id(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _approval_session(session_id="f-list", tool_call_id="call_0", gate_id=G1))
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_graph_two_gate_session(session_id="f-list2"))

    one = await client.get("/v1/workspaces/ws-g/sessions/f-list/yields/pending")
    assert one.status_code == 200, one.text
    assert [i["gate_id"] for i in one.json()["items"]] == [G1]

    two = await client.get("/v1/workspaces/ws-g/sessions/f-list2/yields/pending")
    assert {i["tool_call_id"]: i["gate_id"] for i in two.json()["items"]} == {"call-0": G1, "call-1": G2}

    inbox = await client.get("/v1/yields/pending", params={"workspace_id": "ws-g"})
    assert inbox.status_code == 200, inbox.text
    rows = {r["session_id"]: r for r in inbox.json()["items"]}
    assert rows["f-list"]["gate_id"] == G1
    assert rows["f-list2"]["gate_id"] == G1          # the primary gate's, same as its tool_call_id


# ---- ask_user --------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_user_serves_the_gate_id_and_accepts_a_respond_naming_it(app, client):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_ask_user_session(session_id="a-ok", tool_call_id="call_0", gate_id=G1))

    pending = await client.get("/v1/sessions/a-ok/ask_user/pending")
    assert pending.status_code == 200, pending.text
    assert pending.json()["gate_id"] == G1

    resp = await client.post("/v1/sessions/a-ok/ask_user/respond", json={"tool_call_id": "call_0", "gate_id": G1, "response": "EUR"})
    assert resp.status_code == 202, resp.text
    assert (await storage.get("a-ok")).parked_status == "resumable"
    assert _count("ask_user", "matched") == 1


@pytest.mark.asyncio
async def test_a_stale_ask_user_card_for_a_repeated_raw_id_is_409_and_moves_nothing(app, client):
    """The same hole as the approval: an answer meant for round 1's question must not answer round 3's under the same raw id."""
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_ask_user_session(session_id="a-stale", tool_call_id="call_0", gate_id=G2))

    resp = await client.post("/v1/sessions/a-stale/ask_user/respond", json={"tool_call_id": "call_0", "gate_id": G1, "response": "EUR"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["extensions"]["code"] == "approval_stale"
    row = await storage.get("a-stale")
    assert row.parked_status == "parked" and "resume_event_payload" not in (row.parked_state or {})
    assert _count("ask_user", "stale") == 1


@pytest.mark.asyncio
async def test_a_tokenless_ask_user_respond_is_accepted_and_counted(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _ask_user_session(session_id="a-bare", tool_call_id="call_0", gate_id=G1))

    resp = await client.post("/v1/sessions/a-bare/ask_user/respond", json={"tool_call_id": "call_0", "response": "EUR"})

    assert resp.status_code == 202, resp.text
    assert _count("ask_user", "absent") == 1


@pytest.mark.asyncio
async def test_a_malformed_ask_user_gate_id_is_422(app, client):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(
        _ask_user_session(session_id="a-bad", tool_call_id="call_0", gate_id=G1))

    resp = await client.post("/v1/sessions/a-bad/ask_user/respond", json={"tool_call_id": "call_0", "gate_id": "nope", "response": "EUR"})

    assert resp.status_code == 422, resp.text
