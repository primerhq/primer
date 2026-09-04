"""Durable resume-flip in the reply handlers (arch review D-C2).

The ``ask_user`` and ``tool_approval`` respond endpoints used to be
bus-only: they published the reply on the event bus and returned 202 with
NO durable write. The durable ``parked -> resumable`` stamp happened only
in the bus listener, so a reply that landed while the listener was
down/reconnecting was permanently lost (LISTEN/NOTIFY is not durable) and
the park later timed out "unanswered".

These tests wire the app WITHOUT any :class:`YieldEventListener`, so the
bus publish is a genuine no-op (nothing consumes it). If the handler still
flips + stamps the row, the durability fix is doing its job. They also
prove the flip survives a publish that raises (best-effort wake) and is
idempotent under a second, listener-style flip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.int.claim import ClaimKind
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.yields import durably_mark_session_resumable, flip_sessions_parked_on


class _FlakyEngine:
    """Minimal ClaimEngine stand-in that records the leases it upserts.

    ``mark_resumable`` raises on its first ``fail_times`` calls, reproducing
    the non-atomic flip: ``storage.update`` lands (row -> ``resumable``) but
    the lease re-arm blows up, leaving NO lease row. ``claim_due`` JOINs the
    leases table, so such a row is permanently unclaimable.
    """

    def __init__(self, *, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.calls: list[tuple[ClaimKind, str]] = []
        self.leases: set[tuple[ClaimKind, str]] = set()

    async def mark_resumable(
        self, kind: ClaimKind, entity_id: str, *, priority: int = 50,
    ) -> None:
        self.calls.append((kind, entity_id))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("lease upsert down")
        self.leases.add((kind, entity_id))


def _make_ask_user_parked_session(
    *, session_id: str, tool_call_id: str,
) -> WorkspaceSession:
    now = datetime.now(UTC)
    ek = f"ask_user:{session_id}:{tool_call_id}"
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="ws-x",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=now,
    )
    sess.parked_status = "parked"
    sess.parked_event_key = ek
    sess.parked_until = now + timedelta(seconds=600)
    sess.parked_at = now
    sess.parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "ask_user",
            "event_key": ek,
            "timeout": 600.0,
            "resume_metadata": {
                "prompt": "name?",
                "response_schema": None,
                "tool_call_id": tool_call_id,
                "parked_at_iso": now.isoformat(),
            },
        },
        "llm_messages": [],
        "turn_no": 1,
        "started_at": now.isoformat(),
        "resume_event_payload": None,
    }
    return sess


def _make_approval_parked_session(
    *, session_id: str, tool_call_id: str,
) -> WorkspaceSession:
    now = datetime.now(UTC)
    ek = f"tool_approval:{session_id}:{tool_call_id}"
    return WorkspaceSession(
        id=session_id,
        workspace_id="ws",
        binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING,
        created_at=now,
        parked_status="parked",
        parked_at=now,
        parked_event_key=ek,
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {
                "tool_name": "_approval",
                "event_key": ek,
                "resume_metadata": {
                    "original_call": {
                        "id": tool_call_id,
                        "name": "delete_workspace",
                        "arguments": {"id": "ws-x"},
                    },
                },
            },
            "parked_at_iso": now.isoformat(),
        },
    )


@pytest.mark.asyncio
async def test_ask_user_respond_flips_row_without_listener(app, client):
    """No listener is wired, so the bus publish is a no-op; the row must
    still be flipped + stamped by the handler's own durable write."""
    sess = _make_ask_user_parked_session(session_id="d-a1", tool_call_id="tc1")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/d-a1/ask_user/respond",
        json={"tool_call_id": "tc1", "response": "Alice"},
    )
    assert resp.status_code == 202

    row = await storage.get("d-a1")
    assert row is not None
    # Flipped durably by the handler, even though nothing consumed the bus.
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"response": "Alice"}
    assert row.parked_state["resume_event_key"] == "ask_user:d-a1:tc1"


@pytest.mark.asyncio
async def test_ask_user_respond_flips_even_when_publish_raises(app, client):
    """The bus publish is best-effort: a raising publish must not fail the
    reply nor lose the durable flip."""
    sess = _make_ask_user_parked_session(session_id="d-a2", tool_call_id="tc2")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    async def _boom(key, payload):  # noqa: ANN001
        raise RuntimeError("bus down")

    original = app.state.event_bus.publish
    app.state.event_bus.publish = _boom
    try:
        resp = await client.post(
            "/v1/sessions/d-a2/ask_user/respond",
            json={"tool_call_id": "tc2", "response": "Bob"},
        )
        assert resp.status_code == 202
    finally:
        app.state.event_bus.publish = original

    row = await storage.get("d-a2")
    assert row is not None
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"response": "Bob"}


@pytest.mark.asyncio
async def test_tool_approval_respond_flips_row_without_listener(app, client):
    """The tool_approval decision publisher must also stamp the row durably
    before the (unconsumed) bus publish."""
    sess = _make_approval_parked_session(
        session_id="d-ap1", tool_call_id="call-1",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/d-ap1/tool_approval/respond",
        json={"tool_call_id": "call-1", "decision": "approved"},
    )
    assert resp.status_code == 202

    row = await storage.get("d-ap1")
    assert row is not None
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {
        "decision": "approved",
        "reason": None,
        # Stamped by the respond route (P6 approver routing); the
        # fixture client's auto-registered first user is the admin.
        "decided_by": "testuser",
    }


@pytest.mark.asyncio
async def test_tool_approval_respond_writes_a_durable_record_immediately(
    app, client,
):
    """01a068da: the ONE write site used to be the eventual resume
    (session_resume_coordinator.py, best-effort) - a crash between this
    respond and that resume lost the audit record entirely. Now the
    respond route itself writes it durably, before this test's session
    has been resumed at all (nothing here drives a resume)."""
    from primer.model.storage import OffsetPage
    from primer.model.tool_approval import ToolApprovalRecord

    sess = _make_approval_parked_session(
        session_id="d-rec1", tool_call_id="call-rec1",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/d-rec1/tool_approval/respond",
        json={"tool_call_id": "call-rec1", "decision": "approved"},
    )
    assert resp.status_code == 202

    records = app.state.storage_provider.get_storage(ToolApprovalRecord)
    page = await records.list(OffsetPage(offset=0, length=50))
    matches = [r for r in page.items if r.session_id == "d-rec1"]
    assert len(matches) == 1
    rec = matches[0]
    assert rec.decision == "approved"
    assert rec.tool_name == "delete_workspace"
    assert rec.tool_call_id == "call-rec1"
    assert rec.session_id == "d-rec1"
    assert rec.agent_id == "agt"
    # Stamped by the respond route's approver-routing (fixture client's
    # auto-registered first user is the admin, same as the payload
    # assertion above).
    assert rec.decided_by == "testuser"
    assert rec.gate_event_key == "tool_approval:d-rec1:call-rec1"


@pytest.mark.asyncio
async def test_tool_approval_respond_reject_writes_a_record_with_the_reason(
    app, client,
):
    from primer.model.storage import OffsetPage
    from primer.model.tool_approval import ToolApprovalRecord

    sess = _make_approval_parked_session(
        session_id="d-rec2", tool_call_id="call-rec2",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/d-rec2/tool_approval/respond",
        json={
            "tool_call_id": "call-rec2", "decision": "rejected",
            "reason": "too risky",
        },
    )
    assert resp.status_code == 202

    records = app.state.storage_provider.get_storage(ToolApprovalRecord)
    page = await records.list(OffsetPage(offset=0, length=50))
    matches = [r for r in page.items if r.session_id == "d-rec2"]
    assert len(matches) == 1
    assert matches[0].decision == "rejected"
    assert matches[0].reason == "too risky"


@pytest.mark.asyncio
async def test_second_listener_style_flip_is_idempotent(app, client):
    """After the handler's durable flip, a second (listener-style) flip of
    the same single-event park is a guard-rejected no-op - it must not
    corrupt the already-stamped payload."""
    sess = _make_ask_user_parked_session(session_id="d-idem", tool_call_id="tcx")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)

    resp = await client.post(
        "/v1/sessions/d-idem/ask_user/respond",
        json={"tool_call_id": "tcx", "response": "first"},
    )
    assert resp.status_code == 202

    row = await storage.get("d-idem")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"response": "first"}

    # Listener-style replay for the same key: the guard rejects a
    # single-event park that is already resumable, so nothing changes.
    did = await durably_mark_session_resumable(
        row,
        event_key="ask_user:d-idem:tcx",
        payload={"response": "second"},
        session_storage=storage,
        engine=None,
    )
    assert did is False
    after = await storage.get("d-idem")
    assert after.parked_status == "resumable"
    assert after.parked_state["resume_event_payload"] == {"response": "first"}


@pytest.mark.asyncio
async def test_ask_user_retry_repairs_lease_lost_by_a_half_applied_flip(
    app, client,
):
    """The durable flip writes twice and cannot share a transaction: a
    ``mark_resumable`` that raises after ``storage.update`` landed leaves the
    row ``resumable`` with NO lease, which ``claim_due``'s JOIN makes
    permanently unclaimable.

    The first reply must NOT be reported accepted in that state, and a client
    retry must REPAIR the missing lease rather than 202 a session that will
    never resume.
    """
    sess = _make_ask_user_parked_session(session_id="d-rep", tool_call_id="tcr")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    engine = _FlakyEngine(fail_times=1)
    app.state.claim_engine = engine

    # 1st reply: storage.update lands, the lease re-arm raises. The error
    # propagates -- the caller must NOT get a 202 for an unclaimable session.
    with pytest.raises(RuntimeError, match="lease upsert down"):
        await client.post(
            "/v1/sessions/d-rep/ask_user/respond",
            json={"tool_call_id": "tcr", "response": "Ada"},
        )

    # The row is stamped + resumable, but stranded: no lease exists.
    row = await storage.get("d-rep")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"response": "Ada"}
    assert (ClaimKind.SESSION, "d-rep") not in engine.leases

    # 2nd reply (client retry): the flip guard rejects the already-resumable
    # row, but the handler now ACTS on that False and re-drives the
    # idempotent mark_resumable, repairing the lease before returning 202.
    resp = await client.post(
        "/v1/sessions/d-rep/ask_user/respond",
        json={"tool_call_id": "tcr", "response": "Ada"},
    )
    assert resp.status_code == 202
    assert (ClaimKind.SESSION, "d-rep") in engine.leases

    # The retry repaired the lease without corrupting the stamped reply.
    after = await storage.get("d-rep")
    assert after.parked_status == "resumable"
    assert after.parked_state["resume_event_payload"] == {"response": "Ada"}


@pytest.mark.asyncio
async def test_tool_approval_retry_repairs_lease_lost_by_a_half_applied_flip(
    app, client,
):
    """The tool_approval decision path shares the same non-atomic flip, so it
    must repair a lost lease on retry too."""
    sess = _make_approval_parked_session(
        session_id="d-aprep", tool_call_id="call-r",
    )
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    engine = _FlakyEngine(fail_times=1)
    app.state.claim_engine = engine

    with pytest.raises(RuntimeError, match="lease upsert down"):
        await client.post(
            "/v1/sessions/d-aprep/tool_approval/respond",
            json={"tool_call_id": "call-r", "decision": "approved"},
        )

    row = await storage.get("d-aprep")
    assert row.parked_status == "resumable"
    assert (ClaimKind.SESSION, "d-aprep") not in engine.leases

    resp = await client.post(
        "/v1/sessions/d-aprep/tool_approval/respond",
        json={"tool_call_id": "call-r", "decision": "approved"},
    )
    assert resp.status_code == 202
    assert (ClaimKind.SESSION, "d-aprep") in engine.leases


@pytest.mark.asyncio
async def test_healthy_double_reply_still_repairs_idempotently(app, client):
    """The repair is an idempotent upsert, so an ordinary double-reply (lease
    already healthy) stays a harmless no-op that still returns 202."""
    sess = _make_ask_user_parked_session(session_id="d-2x", tool_call_id="tc2x")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    engine = _FlakyEngine(fail_times=0)
    app.state.claim_engine = engine

    first = await client.post(
        "/v1/sessions/d-2x/ask_user/respond",
        json={"tool_call_id": "tc2x", "response": "one"},
    )
    assert first.status_code == 202
    assert (ClaimKind.SESSION, "d-2x") in engine.leases

    second = await client.post(
        "/v1/sessions/d-2x/ask_user/respond",
        json={"tool_call_id": "tc2x", "response": "two"},
    )
    assert second.status_code == 202
    # The guard still protects the first reply's payload from being clobbered.
    after = await storage.get("d-2x")
    assert after.parked_state["resume_event_payload"] == {"response": "one"}


@pytest.mark.asyncio
async def test_flip_never_arms_a_lease_on_a_row_that_already_ended(app):
    """Cross-review race closure: ending a session does NOT clear
    parked_status (only reset.py's reopen / abandon.py clear it), so a
    row a DIFFERENT worker ended between a caller's own status check and
    this publish still reads parked_status="parked" with the matching
    event_key here. Without the fix, flip_sessions_parked_on's find()
    would still pick it up, durably_mark_session_resumable would arm a
    lease on it, and the next claim would hit workspace_executor's
    "cannot invoke ENDED session" guard - reproducing the original crash
    through a narrower window. Exercises the real find() predicates, not
    just the in-memory guard, since the fix is at that query layer."""
    sess = _make_ask_user_parked_session(session_id="d-ended", tool_call_id="tce")
    sess.status = SessionStatus.ENDED
    sess.ended_reason = "completed"
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    engine = _FlakyEngine(fail_times=0)

    flipped = await flip_sessions_parked_on(
        "ask_user:d-ended:tce", {"response": "too late"},
        session_storage=storage, engine=engine,
    )

    assert flipped == 0
    assert engine.calls == [], "no lease may be armed on an ended row"
    row = await storage.get("d-ended")
    assert row.parked_status == "parked", "the stale row must be left untouched"


@pytest.mark.asyncio
async def test_flip_rejects_a_row_ended_after_the_caller_snapshotted_it(app):
    """The narrower TOCTOU the test above does NOT cover: a row that ends
    IN THE GAP between a caller's read and this write, not before it.

    Simulates the interleaving directly: `sess` is the caller's own
    snapshot, taken while the row was still RUNNING (not ENDED) - so the
    OLD snapshot-check (`if session.status == ENDED: return False`) would
    have read False and let the write through. Between that read and this
    call, a DIFFERENT worker independently ends the row in storage. The
    fix is `Storage.update_unless` evaluating "is status ENDED" against
    the row's CURRENT value at write time, not the stale `sess` argument -
    so this must still reject even though `sess.status` says RUNNING.
    """
    sess = _make_ask_user_parked_session(session_id="d-race", tool_call_id="tcz")
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    assert sess.status == SessionStatus.RUNNING

    # The interleaving: a different worker ends the row AFTER `sess` was
    # snapshotted (sess itself, held by this test, is never touched -
    # exactly mirroring a caller whose in-memory copy is now stale).
    ended = sess.model_copy(update={
        "status": SessionStatus.ENDED, "ended_reason": "completed",
    })
    await storage.update(ended)

    engine = _FlakyEngine(fail_times=0)
    did = await durably_mark_session_resumable(
        sess,  # the STALE snapshot - still status=RUNNING
        event_key="ask_user:d-race:tcz",
        payload={"response": "too late"},
        session_storage=storage,
        engine=engine,
    )

    assert did is False, "a row ended mid-flight must still be rejected"
    assert engine.calls == [], "no lease may be armed on a row that ended mid-flight"
    row = await storage.get("d-race")
    assert row.status == SessionStatus.ENDED
    assert row.parked_status == "parked", "the durable flip must not have landed"
