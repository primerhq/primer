"""Integration tests for the /v1/sessions/{id}/{ask_user,yields} surface.

Covers M3 of the yielding-tools feature: the ask_user pending/respond
endpoints + the tool-agnostic cancel-yielded-tool endpoint.

The fixture wires a real in-memory EventBus + the listener so the
end-to-end flow (POST respond → publish → listener → mark_resumable)
actually flips the parked session in the test process.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from primer.api.app import create_test_app
from primer.bus.in_memory import InMemoryEventBus
from primer.bus.listener import YieldEventListener
from primer.model.workspace_session import (
    AgentSessionBinding,
    WorkspaceSession,
    SessionStatus,
)
from primer.scheduler.in_memory import InMemoryScheduler, _LeaseState


@pytest.fixture
def app(
    fake_storage_provider,
    fake_provider_registry,
) -> FastAPI:
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )


@pytest_asyncio.fixture
async def bus_and_listener(app):
    """Wire a real bus + listener to the app's scheduler.

    The endpoint POST publishes to the bus; the listener flips the
    parked session via mark_resumable. Without this, the POST would
    only update the bus and tests would have to read the bus to
    verify (clumsier than asserting on the row state).
    """
    bus = InMemoryEventBus()
    await bus.initialize()
    app.state.event_bus = bus
    scheduler: InMemoryScheduler = app.state.scheduler
    listener = YieldEventListener(bus=bus, scheduler=scheduler)
    listener.start()
    try:
        yield bus, listener
    finally:
        await listener.stop()
        await bus.aclose()


@pytest_asyncio.fixture
async def client(app, bus_and_listener):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as c:
        yield c


def _make_parked_session(
    *,
    session_id: str,
    tool_call_id: str,
    prompt: str = "What is your name?",
    response_schema: dict | None = None,
    parked_until: datetime | None = None,
) -> WorkspaceSession:
    sess = WorkspaceSession(
        id=session_id,
        workspace_id="ws-x",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
    )
    now = datetime.now(timezone.utc)
    sess.parked_status = "parked"
    sess.parked_event_key = f"ask_user:{session_id}:{tool_call_id}"
    sess.parked_until = parked_until or (now + timedelta(seconds=600))
    sess.parked_at = now
    sess.parked_state = {
        "schema_version": 1,
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": "ask_user",
            "event_key": f"ask_user:{session_id}:{tool_call_id}",
            "timeout": 600.0,
            "resume_metadata": {
                "prompt": prompt,
                "response_schema": response_schema,
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


async def _seed_session(app, sess: WorkspaceSession) -> None:
    """Insert into both the storage row AND the in-memory scheduler dict.

    Storage is what the GET endpoint reads; the scheduler dict is what
    mark_resumable mutates. Production keeps them in sync via the
    scheduler writing through to storage; tests inject directly.
    """
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(sess)
    scheduler: InMemoryScheduler = app.state.scheduler
    scheduler._sessions[sess.id] = sess
    scheduler._leases[sess.id] = _LeaseState(
        worker_id=None,
        expires_at=None,
        runnable=False,
        next_attempt_at=datetime.now(timezone.utc),
    )


# ===========================================================================
# GET /v1/sessions/{id}/ask_user/pending
# ===========================================================================


@pytest.mark.asyncio
class TestAskUserPending:
    async def test_pending_returns_prompt_for_parked_session(
        self, app, client,
    ):
        sess = _make_parked_session(
            session_id="sess-1",
            tool_call_id="tc-1",
            prompt="What is your name?",
        )
        await _seed_session(app, sess)
        resp = await client.get("/v1/sessions/sess-1/ask_user/pending")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tool_call_id"] == "tc-1"
        assert body["prompt"] == "What is your name?"
        assert body["response_schema"] is None
        assert "parked_at" in body

    async def test_pending_returns_response_schema_when_present(
        self, app, client,
    ):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        sess = _make_parked_session(
            session_id="sess-2",
            tool_call_id="tc-2",
            response_schema=schema,
        )
        await _seed_session(app, sess)
        resp = await client.get("/v1/sessions/sess-2/ask_user/pending")
        assert resp.status_code == 200
        assert resp.json()["response_schema"] == schema

    async def test_pending_404_when_session_has_no_park(
        self, app, client,
    ):
        sess = WorkspaceSession(
            id="sess-np",
            workspace_id="ws-x",
            binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
        await _seed_session(app, sess)
        resp = await client.get("/v1/sessions/sess-np/ask_user/pending")
        assert resp.status_code == 404

    async def test_pending_404_for_unknown_session(self, app, client):
        resp = await client.get("/v1/sessions/does-not-exist/ask_user/pending")
        assert resp.status_code == 404

    async def test_pending_404_when_park_is_not_ask_user(self, app, client):
        # A sleep park has no prompt — the endpoint must not leak it.
        sess = WorkspaceSession(
            id="sess-sl",
            workspace_id="ws-x",
            binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
        sess.parked_status = "parked"
        sess.parked_event_key = "timer:tc-sl"
        sess.parked_until = datetime.now(timezone.utc) + timedelta(seconds=30)
        sess.parked_at = datetime.now(timezone.utc)
        sess.parked_state = {
            "schema_version": 1,
            "tool_call_id": "tc-sl",
            "yielded": {
                "tool_name": "sleep",
                "event_key": "timer:tc-sl",
                "timeout": 30.0,
                "resume_metadata": {"requested_seconds": 30.0},
            },
            "llm_messages": [],
            "turn_no": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "resume_event_payload": None,
        }
        await _seed_session(app, sess)
        resp = await client.get("/v1/sessions/sess-sl/ask_user/pending")
        assert resp.status_code == 404


# ===========================================================================
# POST /v1/sessions/{id}/ask_user/respond
# ===========================================================================


@pytest.mark.asyncio
class TestAskUserRespond:
    async def test_respond_publishes_and_flips_parked_to_resumable(
        self, app, client,
    ):
        sess = _make_parked_session(session_id="sess-r", tool_call_id="tc-r")
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-r/ask_user/respond",
            json={"tool_call_id": "tc-r", "response": "Alice"},
        )
        assert resp.status_code == 202
        # Let the listener observe the event + flip the row.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sess.parked_status == "resumable":
                break
        assert sess.parked_status == "resumable"
        assert sess.parked_state["resume_event_payload"] == {"response": "Alice"}

    async def test_respond_accepts_complex_response(self, app, client):
        sess = _make_parked_session(
            session_id="sess-rc",
            tool_call_id="tc-rc",
        )
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-rc/ask_user/respond",
            json={"tool_call_id": "tc-rc", "response": {"k": "v", "n": 7}},
        )
        assert resp.status_code == 202
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sess.parked_status == "resumable":
                break
        assert sess.parked_state["resume_event_payload"] == {
            "response": {"k": "v", "n": 7}
        }

    async def test_respond_404_when_tool_call_id_does_not_match(
        self, app, client,
    ):
        sess = _make_parked_session(
            session_id="sess-mm",
            tool_call_id="tc-real",
        )
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-mm/ask_user/respond",
            json={"tool_call_id": "tc-wrong", "response": "ok"},
        )
        assert resp.status_code == 404
        assert sess.parked_status == "parked"

    async def test_respond_404_when_session_has_no_park(self, app, client):
        sess = WorkspaceSession(
            id="sess-nopark",
            workspace_id="ws-x",
            binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-nopark/ask_user/respond",
            json={"tool_call_id": "tc-x", "response": "ok"},
        )
        assert resp.status_code == 404

    async def test_respond_422_when_response_violates_schema(
        self, app, client,
    ):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        sess = _make_parked_session(
            session_id="sess-sc",
            tool_call_id="tc-sc",
            response_schema=schema,
        )
        await _seed_session(app, sess)
        # Response missing required "name" field — must 422 and NOT flip.
        resp = await client.post(
            "/v1/sessions/sess-sc/ask_user/respond",
            json={"tool_call_id": "tc-sc", "response": {"wrong": "field"}},
        )
        assert resp.status_code == 422
        await asyncio.sleep(0.1)
        assert sess.parked_status == "parked"

    async def test_respond_succeeds_when_response_satisfies_schema(
        self, app, client,
    ):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        sess = _make_parked_session(
            session_id="sess-sok",
            tool_call_id="tc-sok",
            response_schema=schema,
        )
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-sok/ask_user/respond",
            json={"tool_call_id": "tc-sok", "response": {"name": "Alice"}},
        )
        assert resp.status_code == 202


# ===========================================================================
# POST /v1/sessions/{id}/yields/{tool_call_id}/cancel
# ===========================================================================


@pytest.mark.asyncio
class TestCancelYieldedTool:
    async def test_cancel_publishes_marker_and_flips_to_resumable(
        self, app, client,
    ):
        sess = _make_parked_session(
            session_id="sess-c", tool_call_id="tc-c",
        )
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-c/yields/tc-c/cancel",
            json={"reason": "operator skipped"},
        )
        assert resp.status_code == 202
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sess.parked_status == "resumable":
                break
        assert sess.parked_status == "resumable"
        payload = sess.parked_state["resume_event_payload"]
        assert payload.get("__yield_cancelled__") is True
        assert payload.get("reason") == "operator skipped"

    async def test_cancel_works_without_reason(self, app, client):
        sess = _make_parked_session(
            session_id="sess-cn", tool_call_id="tc-cn",
        )
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-cn/yields/tc-cn/cancel",
            json={},
        )
        assert resp.status_code == 202
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sess.parked_status == "resumable":
                break
        assert sess.parked_state["resume_event_payload"].get("reason") is None

    async def test_cancel_404_when_tool_call_id_does_not_match(
        self, app, client,
    ):
        sess = _make_parked_session(
            session_id="sess-cm", tool_call_id="tc-real",
        )
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-cm/yields/tc-wrong/cancel",
            json={"reason": "skip"},
        )
        assert resp.status_code == 404

    async def test_cancel_404_when_session_has_no_park(self, app, client):
        sess = WorkspaceSession(
            id="sess-np2",
            workspace_id="ws-x",
            binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-np2/yields/tc-x/cancel",
            json={"reason": "x"},
        )
        assert resp.status_code == 404

    async def test_cancel_409_when_session_cancel_already_requested(
        self, app, client,
    ):
        sess = _make_parked_session(
            session_id="sess-409", tool_call_id="tc-409",
        )
        sess.cancel_requested = True
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-409/yields/tc-409/cancel",
            json={"reason": "x"},
        )
        assert resp.status_code == 409

    async def test_cancel_works_for_sleep_yield_too(self, app, client):
        # Cancel-yielded-tool must be tool-agnostic — works for sleep
        # parks the same as ask_user parks.
        sess = WorkspaceSession(
            id="sess-slc",
            workspace_id="ws-x",
            binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
            status=SessionStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
        )
        sess.parked_status = "parked"
        sess.parked_event_key = "timer:tc-slc"
        sess.parked_until = datetime.now(timezone.utc) + timedelta(seconds=60)
        sess.parked_at = datetime.now(timezone.utc)
        sess.parked_state = {
            "schema_version": 1,
            "tool_call_id": "tc-slc",
            "yielded": {
                "tool_name": "sleep",
                "event_key": "timer:tc-slc",
                "timeout": 60.0,
                "resume_metadata": {"requested_seconds": 60.0},
            },
            "llm_messages": [],
            "turn_no": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "resume_event_payload": None,
        }
        await _seed_session(app, sess)
        resp = await client.post(
            "/v1/sessions/sess-slc/yields/tc-slc/cancel",
            json={"reason": "skip"},
        )
        assert resp.status_code == 202
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sess.parked_status == "resumable":
                break
        assert sess.parked_status == "resumable"
        assert (
            sess.parked_state["resume_event_payload"].get("__yield_cancelled__")
            is True
        )
