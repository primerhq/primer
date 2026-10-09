"""A decision's wake names the gate it decided, so a wake delivered again cannot decide a later gate (console review C-033 round 2, PR 4).

The wake of a human decision is delivered by event key alone and at least once (the bus and the event log's dispatcher both replay): a ``session.wake``
redelivered after the session resumed and PARKED AGAIN under the same provider id (same event key, a new gate) flipped the NEW gate with the old decision.
Every human-decision wake now carries ``__yield_gate_id__`` (the id of the gate that was resolved when the decision was made, whether or not the request
named it), and ``durably_mark_session_resumable`` refuses a wake whose id is set and is not the pending gate's. A wake with no id (written before this
release) and a pending gate with no id are judged as before. The key has the primer-internal ``__yield_`` prefix, so the resume classification strips it
and no hook or approval classifier ever sees it.
"""

from __future__ import annotations

import pytest

from primer.model.workspace_session import WorkspaceSession
from tests.api.test_gate_fence import G1, G2, _approval_session, _ask_user_session, _count, _fresh_metrics  # noqa: F401
from tests.api.test_gate_fence_cancel import _Published, _url
from tests.api.test_gate_fence_round2 import _graph_ask_user_session

WAKE_KEY = "__yield_gate_id__"


async def _wakes(app) -> list:
    store = app.state.storage_provider.get_event_store()
    return [e for e in await store.read_after(0, limit=10_000) if e.event_type == "session.wake"]


async def _redeliver(app, wake) -> None:
    from primer.events.dispatcher import EventDispatcher

    await EventDispatcher(storage_provider=app.state.storage_provider, claim_engine=None)._deliver_flip(wake)


# ---- the replay probe ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_replayed_approval_wake_does_not_decide_a_later_gate(app, client):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_approval_session(session_id="r-appr", tool_call_id="call_0", gate_id=G1))
    resp = await client.post("/v1/sessions/r-appr/tool_approval/respond", json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})
    assert resp.status_code == 202, resp.text
    [wake] = await _wakes(app)
    # round 3: the session resumed and parked again under the same raw id: a new gate nobody has decided
    await storage.update(_approval_session(session_id="r-appr", tool_call_id="call_0", gate_id=G2))

    await _redeliver(app, wake)

    row = await storage.get("r-appr")
    assert row.parked_status == "parked", "round 1's decision decided round 3's gate"
    assert "resume_event_payload" not in (row.parked_state or {})


@pytest.mark.asyncio
async def test_a_replayed_ask_user_wake_does_not_answer_a_later_question(app, client):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_ask_user_session(session_id="r-ask", tool_call_id="call_0", gate_id=G1))
    resp = await client.post("/v1/sessions/r-ask/ask_user/respond", json={"tool_call_id": "call_0", "gate_id": G1, "response": "EUR"})
    assert resp.status_code == 202, resp.text
    [wake] = await _wakes(app)
    await storage.update(_ask_user_session(session_id="r-ask", tool_call_id="call_0", gate_id=G2))

    await _redeliver(app, wake)

    row = await storage.get("r-ask")
    assert row.parked_status == "parked", "round 1's answer answered round 3's question"


@pytest.mark.asyncio
async def test_a_wake_redelivered_to_the_gate_it_decided_is_the_idempotent_no_op_it_was(app, client):
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_approval_session(session_id="r-same", tool_call_id="call_0", gate_id=G1))
    await client.post("/v1/sessions/r-same/tool_approval/respond", json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})
    [wake] = await _wakes(app)
    before = await storage.get("r-same")

    await _redeliver(app, wake)

    after = await storage.get("r-same")
    assert after.parked_status == "resumable" and after.parked_state == before.parked_state


@pytest.mark.asyncio
async def test_a_wake_written_before_this_release_still_applies(app, client):
    """No id in the wake payload (the event log holds wakes from before): judged by the event key alone, as it always was."""
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_approval_session(session_id="r-old", tool_call_id="call_0", gate_id=G1))
    resp = await client.post("/v1/sessions/r-old/tool_approval/respond", json={"tool_call_id": "call_0", "gate_id": G1, "decision": "approved"})
    assert resp.status_code == 202, resp.text
    [wake] = await _wakes(app)
    payload = dict(wake.payload["wake_payload"])
    payload.pop(WAKE_KEY, None)
    legacy = wake.model_copy(update={"payload": {**wake.payload, "wake_payload": payload}})
    await storage.update(_approval_session(session_id="r-old", tool_call_id="call_0", gate_id=G2))

    await _redeliver(app, legacy)

    assert (await storage.get("r-old")).parked_status == "resumable"


# ---- every human-decision wake names its gate ------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"gate_id": G1}, {}], ids=["named", "tokenless"])
async def test_the_approval_wake_carries_the_gate_that_was_resolved(app, client, body):
    """A tokenless decision resolves the pending gate; the wake names it all the same, so even that decision cannot be replayed onto a later gate."""
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_approval_session(session_id="w-appr", tool_call_id="call_0", gate_id=G1))

    resp = await client.post("/v1/sessions/w-appr/tool_approval/respond", json={"tool_call_id": "call_0", "decision": "approved", **body})

    assert resp.status_code == 202, resp.text
    [wake] = await _wakes(app)
    assert wake.payload["wake_payload"][WAKE_KEY] == G1
    assert wake.payload["wake_payload"]["decision"] == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"gate_id": G1}, {}], ids=["named", "tokenless"])
async def test_the_ask_user_wake_carries_the_gate_that_was_resolved(app, client, body):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_ask_user_session(session_id="w-ask", tool_call_id="call_0", gate_id=G1))

    resp = await client.post("/v1/sessions/w-ask/ask_user/respond", json={"tool_call_id": "call_0", "response": "EUR", **body})

    assert resp.status_code == 202, resp.text
    [wake] = await _wakes(app)
    assert wake.payload["wake_payload"][WAKE_KEY] == G1 and wake.payload["wake_payload"]["response"] == "EUR"


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["agent", "tool_call"])
async def test_the_graph_ask_user_wake_carries_the_gate_of_the_sibling_that_was_answered(app, client, via):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(_graph_ask_user_session(session_id="w-graph", via=via))

    resp = await client.post("/v1/sessions/w-graph/ask_user/respond", json={"tool_call_id": "dup", "gate_id": G2, "response": "blue"})

    assert resp.status_code == 202, resp.text
    [wake] = await _wakes(app)
    assert wake.payload["wake_payload"][WAKE_KEY] == G2


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_approval_session, _ask_user_session], ids=["approval", "ask_user"])
async def test_a_cancel_of_a_human_gate_names_the_gate_it_cancelled(app, client, make):
    await app.state.storage_provider.get_storage(WorkspaceSession).create(make(session_id="w-cancel", tool_call_id="call_0", gate_id=G1))
    published = _Published(app.state.event_bus)

    resp = await client.post(_url("w-cancel", "call_0"), json={})

    assert resp.status_code == 202, resp.text
    [(_key, payload)] = [e for e in published.events if e[0].endswith("w-cancel:call_0")]
    assert payload[WAKE_KEY] == G1 and payload["__yield_cancelled__"] is True


@pytest.mark.asyncio
async def test_a_cancel_of_a_yield_that_is_not_a_gate_carries_no_gate_id(app, client):
    from tests.api.test_gate_fence_round2 import _yield_session

    await app.state.storage_provider.get_storage(WorkspaceSession).create(_yield_session(session_id="w-sleep", tool_name="sleep"))
    published = _Published(app.state.event_bus)

    resp = await client.post(_url("w-sleep", "call_0"), json={})

    assert resp.status_code == 202, resp.text
    [(_key, payload)] = [e for e in published.events if e[0].endswith("w-sleep:call_0")]
    assert WAKE_KEY not in payload
