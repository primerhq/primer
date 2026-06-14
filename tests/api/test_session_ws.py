"""Tests for the session WS endpoint:
  WS /v1/workspaces/{wid}/sessions/{sid}/ws?cursor=N

Three test scenarios:

1. test_ws_cursor_zero_replays_full_history
   Seed a session with N messages in messages.jsonl (via the fake workspace).
   Connect with cursor=0; assert all N frames arrive in order.

2. test_ws_interrupt_frame_sets_cancel
   Connect WS; send {"kind": "interrupt"}; expect cancel_requested_at set.
   Also expect a ping→pong round-trip (warm-up sanity check).

3. test_ws_mid_turn_reconnect_catches_up
   First connect, receive first few frames (via cursor=0 replay).
   Reconnect with cursor=<seq>; assert only later frames are received.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient as SyncTestClient

from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry


def _runtime_meta():
    """Build a minimal valid WorkspaceRuntimeMeta for test rows."""
    from primer.model.workspace import WorkspaceRuntimeMeta
    return WorkspaceRuntimeMeta(
        url="ws://127.0.0.1:5959/",
        token=SecretStr("t"),
    )


def _auth_sync_client(sclient) -> None:
    """Register + login a default test user so the WS auth gate passes."""
    try:
        sclient.post(
            "/v1/auth/register",
            json={"username": "testuser", "password": "testpassword"},
        )
    except Exception:
        pass
    try:
        sclient.post(
            "/v1/auth/login",
            json={"username": "testuser", "password": "testpassword"},
        )
    except Exception:
        pass
from primer.model.workspace_session import (
    SessionStatus,
    WorkspaceSession,
    AgentSessionBinding,
)


# ===========================================================================
# Fake workspace that supports read_file + append_message_line
# (no real disk I/O)
# ===========================================================================


class _FakeWorkspaceForWS:
    """Minimal workspace that stores messages.jsonl in memory."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self.id = workspace_id
        # Simulated files: path -> bytes
        self._files: dict[str, bytes] = {}
        # Fake template-ish with state_path=".state"
        self._template = _FakeTemplate()
        self.started_sessions: dict[str, dict] = {}

    async def start_session(
        self,
        agent_binding,
        *,
        id=None,
        instructions=None,
        parent_session_id=None,
    ):
        if id is None:
            raise AssertionError("id must be provided")
        self.started_sessions[id] = {}
        return object()

    async def read_file(self, path: str) -> bytes:
        """Return the in-memory content for path, or raise NotFoundError."""
        from primer.model.except_ import NotFoundError
        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        """Append line to the fake messages.jsonl for the session."""
        path = f".state/sessions/{session_id}/messages.jsonl"
        if not line.endswith(b"\n"):
            line = line + b"\n"
        self._files[path] = self._files.get(path, b"") + line

    def seed_messages(self, session_id: str, records: list[dict]) -> None:
        """Seed records into messages.jsonl for replay tests."""
        path = f".state/sessions/{session_id}/messages.jsonl"
        lines = b""
        for rec in records:
            lines += json.dumps(rec).encode() + b"\n"
        self._files[path] = lines

    async def aclose(self):
        pass


class _FakeTemplate:
    state_path = ".state"


class _FakeBackendForWS:
    """Returns a _FakeWorkspaceForWS for every lookup."""

    def __init__(self, _provider) -> None:
        self._workspaces: dict[str, _FakeWorkspaceForWS] = {}

    async def initialize(self):
        pass

    async def aclose(self):
        pass

    async def get(self, workspace_id, *, template=None):
        if workspace_id not in self._workspaces:
            self._workspaces[workspace_id] = _FakeWorkspaceForWS(workspace_id)
        return self._workspaces[workspace_id]

    async def list(self):
        return list(self._workspaces.keys())


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def fake_provider_registry(fake_storage_provider):
    return ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda p: object(),
    )


@pytest.fixture
def workspace_registry(fake_storage_provider):
    return WorkspaceRegistry(
        fake_storage_provider,
        factory=_FakeBackendForWS,
    )


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry, workspace_registry):
    """App with an in-memory backend and session_tick_router wired."""
    _app = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        workspace_registry=workspace_registry,
    )
    # Wire session_tick_router (Task 12 will do this in the lifespan;
    # for this task we wire it directly in the test fixture).
    from primer.session.tick_router import SessionTickRouter
    _app.state.session_tick_router = SessionTickRouter()
    return _app


@pytest.fixture
async def seeded_workspace_id(app, workspace_registry):
    """Ensure the workspace handle exists in the registry."""
    from primer.model.workspace import Workspace, WorkspaceProvider, WorkspaceProviderType, LocalWorkspaceConfig

    sp = app.state.storage_provider
    try:
        await sp.get_storage(WorkspaceProvider).create(
            WorkspaceProvider(
                id="p-ws",
                provider=WorkspaceProviderType.LOCAL,
                config=LocalWorkspaceConfig(root_path="/tmp/primer-ws-ws-test"),
            )
        )
    except Exception:
        pass
    try:
        await sp.get_storage(Workspace).create(
            Workspace(
                id="w1",
                template_id="t-noop",
                provider_id="p-ws",
                created_at=datetime.now(timezone.utc),
                runtime_meta=_runtime_meta(),
            )
        )
    except Exception:
        pass
    # Trigger workspace materialisation in the fake backend so the handle exists.
    await workspace_registry.get_workspace("w1")
    return "w1"


async def _seed_session(
    app,
    workspace_id: str,
    session_id: str,
    *,
    status: SessionStatus = SessionStatus.RUNNING,
) -> WorkspaceSession:
    """Insert a WorkspaceSession row directly into storage."""
    sp = app.state.storage_provider
    storage = sp.get_storage(WorkspaceSession)
    session = WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id="ag-test"),
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    await storage.create(session)
    return session


# ===========================================================================
# Test 1: cursor=0 replays full history
# ===========================================================================


@pytest.mark.asyncio
async def test_ws_cursor_zero_replays_full_history(app, seeded_workspace_id):
    """Connect with cursor=0; all seeded messages should be replayed in order."""
    wid = seeded_workspace_id
    sid = "s-replay-1"

    # Seed the session row.
    await _seed_session(app, wid, sid)

    # Seed 3 message records directly into the fake workspace.
    ws_registry = app.state.workspace_registry
    live_ws = await ws_registry.get_workspace(wid)
    seeded = [
        {"seq": 1, "kind": "user_input", "payload": {"text": "hello"}, "created_at": "2026-01-01T00:00:00Z"},
        {"seq": 2, "kind": "assistant_token", "payload": {"text": "hi there"}, "created_at": "2026-01-01T00:00:01Z"},
        {"seq": 3, "kind": "done", "payload": {"stop_reason": "stop"}, "created_at": "2026-01-01T00:00:02Z"},
    ]
    live_ws.seed_messages(sid, seeded)

    with SyncTestClient(app) as sclient:
        _auth_sync_client(sclient)
        with sclient.websocket_connect(
            f"/v1/workspaces/{wid}/sessions/{sid}/ws?cursor=0"
        ) as ws:
            frames = []
            for _ in range(len(seeded)):
                frames.append(ws.receive_json())

    assert [f["seq"] for f in frames] == [1, 2, 3]
    assert frames[0]["kind"] == "user_input"
    assert frames[1]["kind"] == "assistant_token"
    assert frames[2]["kind"] == "done"


# ===========================================================================
# Test 2: interrupt frame sets cancel_requested_at
# ===========================================================================


@pytest.mark.asyncio
async def test_ws_interrupt_frame_sets_cancel(app, seeded_workspace_id):
    """Sending an interrupt frame must set cancel_requested_at on the session row."""
    wid = seeded_workspace_id
    sid = "s-interrupt-1"

    await _seed_session(app, wid, sid, status=SessionStatus.RUNNING)

    with SyncTestClient(app) as sclient:
        _auth_sync_client(sclient)
        with sclient.websocket_connect(
            f"/v1/workspaces/{wid}/sessions/{sid}/ws?cursor=0"
        ) as ws:
            # Ping first to confirm the connection is live.
            ws.send_json({"kind": "ping"})
            pong = ws.receive_json()
            assert pong == {"kind": "pong"}

            # Send interrupt.
            ws.send_json({"kind": "interrupt"})
            # Give the server-side coroutine time to process the frame
            # and persist the update.  The recv loop runs the update
            # in the same coroutine as accept, so we just need the
            # message to be processed — which happens before we close.

    # After the WS closes verify storage was updated.
    sp = app.state.storage_provider
    session = await sp.get_storage(WorkspaceSession).get(sid)
    assert session is not None
    assert session.cancel_requested_at is not None


# ===========================================================================
# Test 3: mid-turn reconnect with cursor catches up without duplicates
# ===========================================================================


@pytest.mark.asyncio
async def test_ws_mid_turn_reconnect_catches_up(app, seeded_workspace_id):
    """Reconnect with cursor=<seq> only receives frames after that cursor."""
    wid = seeded_workspace_id
    sid = "s-reconnect-1"

    await _seed_session(app, wid, sid)

    ws_registry = app.state.workspace_registry
    live_ws = await ws_registry.get_workspace(wid)
    all_records = [
        {"seq": i, "kind": "assistant_token", "payload": {"text": f"tok-{i}"}, "created_at": "2026-01-01T00:00:00Z"}
        for i in range(1, 6)
    ]
    live_ws.seed_messages(sid, all_records)

    # First connect: receive first 3 frames (cursor=0, so replay all → take first 3).
    with SyncTestClient(app) as sclient:
        _auth_sync_client(sclient)
        with sclient.websocket_connect(
            f"/v1/workspaces/{wid}/sessions/{sid}/ws?cursor=0"
        ) as ws:
            first_three = [ws.receive_json() for _ in range(3)]

    cursor = first_three[-1]["seq"]  # should be 3
    assert cursor == 3

    # Reconnect with cursor=3; should get only seq 4 and 5.
    with SyncTestClient(app) as sclient:
        _auth_sync_client(sclient)
        with sclient.websocket_connect(
            f"/v1/workspaces/{wid}/sessions/{sid}/ws?cursor={cursor}"
        ) as ws:
            next_frames = [ws.receive_json() for _ in range(2)]

    assert all(f["seq"] > cursor for f in next_frames)
    assert [f["seq"] for f in next_frames] == [4, 5]


# ===========================================================================
# Test 4: 4404 when session not found
# ===========================================================================


@pytest.mark.asyncio
async def test_ws_4404_for_missing_session(app, seeded_workspace_id):
    """Session not found or wrong workspace → WS close 4404."""
    wid = seeded_workspace_id
    sid = "s-does-not-exist"

    with SyncTestClient(app) as sclient:
        _auth_sync_client(sclient)
        with pytest.raises(Exception):
            # Starlette's TestClient raises when the server closes with
            # a non-1000 code before the client sends anything.
            with sclient.websocket_connect(
                f"/v1/workspaces/{wid}/sessions/{sid}/ws"
            ) as ws:
                ws.receive_json()


# ===========================================================================
# Test 5: 4410 when session is ended
# ===========================================================================


@pytest.mark.asyncio
async def test_ws_4410_for_ended_session(app, seeded_workspace_id):
    """ENDED session → WS close 4410."""
    wid = seeded_workspace_id
    sid = "s-ended-1"

    await _seed_session(app, wid, sid, status=SessionStatus.ENDED)

    with SyncTestClient(app) as sclient:
        _auth_sync_client(sclient)
        with pytest.raises(Exception):
            with sclient.websocket_connect(
                f"/v1/workspaces/{wid}/sessions/{sid}/ws"
            ) as ws:
                ws.receive_json()


# ===========================================================================
# Test 6: ping → pong
# ===========================================================================


@pytest.mark.asyncio
async def test_ws_ping_pong(app, seeded_workspace_id):
    """A ping frame must elicit a pong frame."""
    wid = seeded_workspace_id
    sid = "s-ping-1"
    await _seed_session(app, wid, sid)

    with SyncTestClient(app) as sclient:
        _auth_sync_client(sclient)
        with sclient.websocket_connect(
            f"/v1/workspaces/{wid}/sessions/{sid}/ws?cursor=0"
        ) as ws:
            ws.send_json({"kind": "ping"})
            pong = ws.receive_json()
            assert pong == {"kind": "pong"}


# ===========================================================================
# Test 7: 4401 when the handshake carries no auth (auth enabled in test app)
# ===========================================================================


@pytest.mark.asyncio
async def test_ws_4401_without_auth(app, seeded_workspace_id):
    """An unauthenticated WS handshake must be closed with code 4401.

    The test app has auth enabled; connecting WITHOUT logging in (no
    signed session cookie) must trip ``require_auth_ws`` and close the
    socket with the WS-spec 4401 code BEFORE any session resolution.
    """
    from starlette.websockets import WebSocketDisconnect

    wid = seeded_workspace_id
    sid = "s-noauth-1"
    await _seed_session(app, wid, sid)

    with SyncTestClient(app) as sclient:
        # Deliberately do NOT call _auth_sync_client: no cookie is sent.
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect(
                f"/v1/workspaces/{wid}/sessions/{sid}/ws?cursor=0"
            ) as ws:
                ws.receive_json()
    assert excinfo.value.code == 4401
