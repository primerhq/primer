"""Tests for the workspace events-history backfill endpoint:

    GET /v1/workspaces/{workspace_id}/events?limit=N

Part C of the studio-activity rework. The workspace tap SSE stream connects
live-from-now, so a panel opened after events happened sees nothing; this
bounded REST endpoint returns the most-recent N events across ALL of the
workspace's sessions (wire-shape TapEvents) so the panel can seed its stream on
open, then tail the live tap and dedupe by (session_id, seq).

Exercised in-process through the real FastAPI app + fake in-memory storage +
a fake workspace backend exposing ``read_file`` over an in-memory
``messages.jsonl`` — mirrors tests/api/test_workspace_tap_sse.py's fakes but
drives a plain (non-streaming) REST request.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


# ===========================================================================
# Fakes — in-memory workspace (messages.jsonl), backend, registry.
# ===========================================================================


class _FakeTemplate:
    state_path = ".state"


class _FakeWorkspaceForEvents:
    state_path = ".state"

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self.id = workspace_id
        self._files: dict[str, bytes] = {}
        self._template = _FakeTemplate()

    async def read_file(self, path: str) -> bytes:
        from primer.model.except_ import NotFoundError

        if path not in self._files:
            raise NotFoundError(f"{path!r} not found")
        return self._files[path]

    def append_record(
        self,
        session_id: str,
        *,
        seq: int,
        kind: str,
        created_at: datetime,
        **payload,
    ) -> None:
        path = f".state/sessions/{session_id}/messages.jsonl"
        rec = {
            "seq": seq,
            "kind": kind,
            "payload": payload,
            "created_at": created_at.isoformat(),
        }
        self._files[path] = self._files.get(path, b"") + (
            json.dumps(rec).encode() + b"\n"
        )

    async def aclose(self) -> None:
        pass


class _FakeBackendForEvents:
    def __init__(self, _provider) -> None:
        self._workspaces: dict[str, _FakeWorkspaceForEvents] = {}

    async def initialize(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def get(self, workspace_id, *, template=None):
        if workspace_id not in self._workspaces:
            self._workspaces[workspace_id] = _FakeWorkspaceForEvents(workspace_id)
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
    return WorkspaceRegistry(fake_storage_provider, factory=_FakeBackendForEvents)


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry, workspace_registry):
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        workspace_registry=workspace_registry,
    )


async def _ensure_workspace(app, workspace_registry, wid: str) -> _FakeWorkspaceForEvents:
    from primer.model.workspace import (
        LocalWorkspaceConfig,
        Workspace,
        WorkspaceProvider,
        WorkspaceProviderType,
        WorkspaceRuntimeMeta,
    )

    sp = app.state.storage_provider
    try:
        await sp.get_storage(WorkspaceProvider).create(
            WorkspaceProvider(
                id="p-ws",
                provider=WorkspaceProviderType.LOCAL,
                config=LocalWorkspaceConfig(root_path="/tmp/primer-events-test"),
            )
        )
    except Exception:
        pass
    try:
        await sp.get_storage(Workspace).create(
            Workspace(
                id=wid,
                template_id="t-noop",
                provider_id="p-ws",
                created_at=_NOW,
                runtime_meta=WorkspaceRuntimeMeta(
                    url="ws://127.0.0.1:5959/", token=SecretStr("t")
                ),
            )
        )
    except Exception:
        pass
    return await workspace_registry.get_workspace(wid)


async def _seed_session(app, *, workspace_id: str, session_id: str) -> None:
    sp = app.state.storage_provider
    session = WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=_NOW,
        last_seq=0,
    )
    await sp.get_storage(WorkspaceSession).create(session)


async def _client(app) -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        await c.post(
            "/v1/auth/register",
            json={"username": "eventsuser", "password": "eventspassword"},
        )
    except Exception:
        pass
    resp = await c.post(
        "/v1/auth/login",
        json={"username": "eventsuser", "password": "eventspassword"},
    )
    assert resp.status_code == 200, resp.text
    return c


# ===========================================================================
# Tests
# ===========================================================================


@pytest_asyncio.fixture
async def seeded(app, workspace_registry):
    """Workspace w1 with two sessions whose events interleave in time."""
    wid = "w1"
    ws = await _ensure_workspace(app, workspace_registry, wid)
    await _seed_session(app, workspace_id=wid, session_id="s1")
    await _seed_session(app, workspace_id=wid, session_id="s2")

    t = _NOW
    # s1: seq 1..3 at T+1, T+2, T+3
    ws.append_record("s1", seq=1, kind="user_input", created_at=t + timedelta(seconds=1), text="hi")
    ws.append_record("s1", seq=2, kind="assistant_token", created_at=t + timedelta(seconds=2), text="working")
    ws.append_record("s1", seq=3, kind="done", created_at=t + timedelta(seconds=3), stop_reason="end_turn")
    # s2: seq 1..2 at T+1.5, T+2.5 (interleaves with s1)
    ws.append_record("s2", seq=1, kind="tool_call", created_at=t + timedelta(seconds=1.5), id="tc1", arguments={"x": 1})
    ws.append_record("s2", seq=2, kind="tool_result", created_at=t + timedelta(seconds=2.5), call_id="tc1", output={"ok": True})
    return wid


async def test_returns_all_events_sorted_oldest_first(app, seeded):
    c = await _client(app)
    try:
        r = await c.get(f"/v1/workspaces/{seeded}/events?limit=100")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        # 5 events across both sessions, sorted by ts ascending (oldest first).
        keys = [(it["session_id"], it["seq"]) for it in items]
        assert keys == [
            ("s1", 1),
            ("s2", 1),
            ("s1", 2),
            ("s2", 2),
            ("s1", 3),
        ]
    finally:
        await c.aclose()


async def test_items_are_wire_shape_tap_events(app, seeded):
    c = await _client(app)
    try:
        r = await c.get(f"/v1/workspaces/{seeded}/events?limit=100")
        items = r.json()["items"]
        first = items[0]
        # `class` (aliased), ts, seq, session_id, payload — merges 1:1 with live
        # tap frames so the client can dedupe by (session_id, seq).
        assert first["class"] == "user_input"
        assert first["session_id"] == "s1"
        assert first["seq"] == 1
        assert isinstance(first["ts"], str)
        assert first["payload"]["text"] == "hi"
        # tool_call payload carried through verbatim.
        tc = next(it for it in items if it["class"] == "tool_call")
        assert tc["payload"]["arguments"] == {"x": 1}
    finally:
        await c.aclose()


async def test_limit_returns_most_recent(app, seeded):
    c = await _client(app)
    try:
        r = await c.get(f"/v1/workspaces/{seeded}/events?limit=2")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert len(items) == 2
        # The two most-recent by ts: s2#2 (T+2.5) then s1#3 (T+3), oldest-first.
        assert [(it["session_id"], it["seq"]) for it in items] == [("s2", 2), ("s1", 3)]
    finally:
        await c.aclose()


async def test_empty_workspace_returns_empty_items(app, workspace_registry):
    wid = "w-empty"
    await _ensure_workspace(app, workspace_registry, wid)
    c = await _client(app)
    try:
        r = await c.get(f"/v1/workspaces/{wid}/events")
        assert r.status_code == 200, r.text
        assert r.json() == {"items": []}
    finally:
        await c.aclose()


async def test_limit_is_bounded(app, seeded):
    c = await _client(app)
    try:
        # Above the cap → 422 (Query le=500).
        r = await c.get(f"/v1/workspaces/{seeded}/events?limit=100000")
        assert r.status_code == 422
    finally:
        await c.aclose()
