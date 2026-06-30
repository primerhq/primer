"""Tests for the workspace tap SSE endpoint:

    GET /v1/workspaces/{workspace_id}/tap

Exercises the first real surface over the Phase-1 tap spine, in-process,
through the real FastAPI app + the fake in-memory storage provider + the real
:class:`InMemoryEventBus`:

* a tick on the bus drives ``TapEvent`` frames out the SSE stream in order,
  each carrying an ``id:`` cursor and ``data:`` JSON with the right ``class``;
* an ``events: class == tool_call`` selector filters the stream;
* reconnect with the last frame's ``Last-Event-ID`` does NOT re-deliver
  earlier events (gap-free continuation);
* live-from-connect: connecting with NO cursor does not replay pre-existing
  records — only records appended after connect.

httpx's ``ASGITransport`` (0.28) buffers the whole response before returning,
so it cannot drive an open-ended SSE stream. We therefore drive the ASGI app
directly in the test's own event loop via :class:`_SSEConnection`: it runs the
real app (routing → auth middleware → the StreamingResponse generator) as a
task, captures ``http.response.body`` chunks as they are produced, and lets the
test interleave ``bus.publish`` with frame reads — all in ONE loop, so the bus,
the :class:`WorkspaceTapRouter`, and the generator share it (no cross-thread
hazard). Auth uses a real cookie obtained through a normal ASGITransport login.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.bus.in_memory import InMemoryEventBus
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.tap.router import WorkspaceTapRouter


_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
_FRAME_TIMEOUT = 3.0


# ===========================================================================
# Fake workspace (in-memory messages.jsonl), backend, registry — mirrors
# tests/api/test_session_ws.py so the tap reader's read_file/state_path
# surface is satisfied without disk IO.
# ===========================================================================


class _FakeTemplate:
    state_path = ".state"


class _FakeWorkspaceForTap:
    """Minimal workspace exposing read_file + state_path + append_record."""

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

    def append_record(self, session_id: str, *, seq: int, kind: str, **payload) -> None:
        path = f".state/sessions/{session_id}/messages.jsonl"
        rec = {
            "seq": seq,
            "kind": kind,
            "payload": payload,
            "created_at": _NOW.isoformat(),
        }
        self._files[path] = self._files.get(path, b"") + (
            json.dumps(rec).encode() + b"\n"
        )

    async def aclose(self) -> None:
        pass


class _FakeBackendForTap:
    def __init__(self, _provider) -> None:
        self._workspaces: dict[str, _FakeWorkspaceForTap] = {}

    async def initialize(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def get(self, workspace_id, *, template=None):
        if workspace_id not in self._workspaces:
            self._workspaces[workspace_id] = _FakeWorkspaceForTap(workspace_id)
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
        factory=_FakeBackendForTap,
    )


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry, workspace_registry):
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        workspace_registry=workspace_registry,
    )


async def _wire_tap_router(app) -> tuple[InMemoryEventBus, WorkspaceTapRouter]:
    """Build a real bus + WorkspaceTapRouter and stash it on app.state.

    create_test_app does not wire the workspace tap router (the production
    lifespan does); the test owns it so it can publish ticks directly on the
    same event loop the SSE generator runs in.
    """
    bus = InMemoryEventBus()
    await bus.initialize()
    app.state.event_bus = bus
    router = WorkspaceTapRouter(
        bus, app.state.storage_provider.get_storage(WorkspaceSession)
    )
    await router.start()
    app.state.workspace_tap_router = router
    return bus, router


async def _ensure_workspace(app, workspace_registry, wid: str) -> _FakeWorkspaceForTap:
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
                config=LocalWorkspaceConfig(root_path="/tmp/primer-tap-test"),
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


async def _seed_session(
    app,
    *,
    workspace_id: str,
    session_id: str,
    agent_id: str = "ag-1",
    last_seq: int = 0,
    status: SessionStatus = SessionStatus.RUNNING,
) -> WorkspaceSession:
    sp = app.state.storage_provider
    session = WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id=agent_id),
        status=status,
        created_at=_NOW,
        last_seq=last_seq,
    )
    await sp.get_storage(WorkspaceSession).create(session)
    return session


async def _login_cookie(app) -> str:
    """Register + login a test user; return the ``primer_session`` cookie value."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            await client.post(
                "/v1/auth/register",
                json={"username": "tapuser", "password": "tappassword"},
            )
        except Exception:
            pass
        resp = await client.post(
            "/v1/auth/login",
            json={"username": "tapuser", "password": "tappassword"},
        )
        assert resp.status_code == 200, resp.text
        cookie = client.cookies.get("primer_session")
        assert cookie, "login did not set primer_session cookie"
        return cookie


# ===========================================================================
# Direct-ASGI SSE connection (single event loop, real app + generator)
# ===========================================================================


class _Frame:
    __slots__ = ("id", "data")

    def __init__(self, frame_id: str | None, data: dict | None) -> None:
        self.id = frame_id
        self.data = data


class _SSEConnection:
    """Drives one GET tap request through the ASGI app, streaming chunks.

    Runs the app as a task feeding a never-ending ``receive`` (so the request
    body channel stays open like a live connection) and capturing each
    ``http.response.body`` chunk into a queue. ``read_frames`` parses SSE
    ``id:``/``data:`` blocks from the chunk stream, skipping keepalive comments.
    """

    def __init__(self, app, path: str, *, cookie: str, headers=None) -> None:
        self._app = app
        self._path = path
        self._cookie = cookie
        self._extra_headers = headers or {}
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._status: int | None = None
        self._resp_headers: dict[str, str] = {}
        self._started = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._disconnect = asyncio.Event()
        self._buf = ""

    async def __aenter__(self) -> "_SSEConnection":
        raw_path, _, query = self._path.partition("?")
        header_list = [
            (b"host", b"test"),
            (b"cookie", f"primer_session={self._cookie}".encode()),
        ]
        for k, v in self._extra_headers.items():
            header_list.append((k.lower().encode(), v.encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": query.encode(),
            "headers": header_list,
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "app": self._app,
        }

        async def _receive():
            # First call: deliver the (empty) request body. Subsequent calls
            # block until disconnect so the response stays open.
            if not self._disconnect.is_set():
                await self._disconnect.wait()
            return {"type": "http.disconnect"}

        async def _send(message):
            if message["type"] == "http.response.start":
                self._status = message["status"]
                self._resp_headers = {
                    k.decode(): v.decode() for k, v in message.get("headers", [])
                }
                self._started.set()
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    self._chunks.put_nowait(body)

        self._task = asyncio.create_task(self._app(scope, _receive, _send))
        await asyncio.wait_for(self._started.wait(), timeout=_FRAME_TIMEOUT)
        return self

    async def __aexit__(self, *exc) -> None:
        self._disconnect.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    @property
    def status_code(self) -> int:
        assert self._status is not None
        return self._status

    @property
    def content_type(self) -> str:
        return self._resp_headers.get("content-type", "")

    async def read_frames(self, *, count: int, timeout: float = _FRAME_TIMEOUT) -> list[_Frame]:
        frames: list[_Frame] = []
        cur_id: str | None = None

        async def _collect() -> None:
            nonlocal cur_id
            while len(frames) < count:
                # Drain complete lines already buffered.
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if line.startswith(":"):
                        continue  # keepalive comment
                    if line.startswith("id:"):
                        cur_id = line[len("id:"):].strip()
                        continue
                    if line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        frames.append(_Frame(cur_id, json.loads(payload)))
                        cur_id = None
                        if len(frames) >= count:
                            return
                    # blank separator: ignore
                if len(frames) >= count:
                    return
                chunk = await self._chunks.get()
                self._buf += chunk.decode()

        await asyncio.wait_for(_collect(), timeout=timeout)
        return frames


# ===========================================================================
# Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_tap_streams_events_in_order_with_cursors(app, workspace_registry):
    wid, sid = "w-tap", "s-tap"
    ws = await _ensure_workspace(app, workspace_registry, wid)
    await _seed_session(app, workspace_id=wid, session_id=sid, last_seq=0)
    cookie = await _login_cookie(app)
    bus, router = await _wire_tap_router(app)
    try:
        # Two records exist; with last_seq=0 and no cursor, live-from-now starts
        # at the high-water mark (0), so seq 1,2 (both > 0) are delivered.
        ws.append_record(sid, seq=1, kind="user_input")
        ws.append_record(sid, seq=2, kind="tool_call", tool="bash")

        async with _SSEConnection(
            app, f"/v1/workspaces/{wid}/tap", cookie=cookie
        ) as conn:
            assert conn.status_code == 200
            assert conn.content_type.startswith("text/event-stream")
            await asyncio.sleep(0.05)  # let the generator subscribe
            await bus.publish(f"session:{sid}:tick", {"seq": 2})

            frames = await conn.read_frames(count=2)
            assert [f.data["class"] for f in frames] == ["user_input", "tool_call"]
            assert [f.data["seq"] for f in frames] == [1, 2]
            assert frames[1].data["payload"] == {"tool": "bash"}
            # Each frame's id: is the resumable cursor; non-empty + distinct.
            assert frames[0].id and frames[1].id
            assert frames[0].id != frames[1].id
            assert all(f.data["workspace_id"] == wid for f in frames)
    finally:
        await router.aclose()
        await bus.aclose()


@pytest.mark.asyncio
async def test_tap_event_selector_filters_class(app, workspace_registry):
    wid, sid = "w-sel", "s-sel"
    ws = await _ensure_workspace(app, workspace_registry, wid)
    await _seed_session(app, workspace_id=wid, session_id=sid, last_seq=0)
    cookie = await _login_cookie(app)
    bus, router = await _wire_tap_router(app)
    try:
        ws.append_record(sid, seq=1, kind="user_input")
        ws.append_record(sid, seq=2, kind="tool_call", tool="bash")
        ws.append_record(sid, seq=3, kind="tool_result")

        selector = json.dumps(
            {
                "events": {
                    "kind": "predicate",
                    "left": {"kind": "field", "name": "class"},
                    "op": "=",
                    "right": {"kind": "value", "value": "tool_call"},
                }
            }
        )
        from urllib.parse import quote

        async with _SSEConnection(
            app,
            f"/v1/workspaces/{wid}/tap?selector={quote(selector)}",
            cookie=cookie,
        ) as conn:
            assert conn.status_code == 200
            await asyncio.sleep(0.05)
            await bus.publish(f"session:{sid}:tick", {"seq": 3})

            frames = await conn.read_frames(count=1)
            assert [f.data["class"] for f in frames] == ["tool_call"]
            assert frames[0].data["seq"] == 2
    finally:
        await router.aclose()
        await bus.aclose()


@pytest.mark.asyncio
async def test_tap_reconnect_with_last_event_id_is_gap_free(app, workspace_registry):
    wid, sid = "w-recon", "s-recon"
    ws = await _ensure_workspace(app, workspace_registry, wid)
    await _seed_session(app, workspace_id=wid, session_id=sid, last_seq=0)
    cookie = await _login_cookie(app)
    bus, router = await _wire_tap_router(app)
    try:
        ws.append_record(sid, seq=1, kind="user_input")
        ws.append_record(sid, seq=2, kind="tool_call")

        # First connection: consume both frames, capture the last cursor.
        async with _SSEConnection(
            app, f"/v1/workspaces/{wid}/tap", cookie=cookie
        ) as conn:
            await asyncio.sleep(0.05)
            await bus.publish(f"session:{sid}:tick", {"seq": 2})
            frames = await conn.read_frames(count=2)
        last_cursor = frames[-1].id
        assert last_cursor

        # New record lands while disconnected.
        ws.append_record(sid, seq=3, kind="done")

        # Reconnect with Last-Event-ID = last cursor; only seq 3 should come.
        async with _SSEConnection(
            app,
            f"/v1/workspaces/{wid}/tap",
            cookie=cookie,
            headers={"Last-Event-ID": last_cursor},
        ) as conn2:
            await asyncio.sleep(0.05)
            await bus.publish(f"session:{sid}:tick", {"seq": 3})
            frames2 = await conn2.read_frames(count=1)
            assert [f.data["class"] for f in frames2] == ["done"]
            assert frames2[0].data["seq"] == 3
    finally:
        await router.aclose()
        await bus.aclose()


@pytest.mark.asyncio
async def test_tap_live_from_now_skips_preexisting_records(app, workspace_registry):
    wid, sid = "w-live", "s-live"
    ws = await _ensure_workspace(app, workspace_registry, wid)
    # Session already has 2 durable records; its high-water mark is last_seq=2.
    ws.append_record(sid, seq=1, kind="user_input")
    ws.append_record(sid, seq=2, kind="tool_call")
    await _seed_session(app, workspace_id=wid, session_id=sid, last_seq=2)
    cookie = await _login_cookie(app)
    bus, router = await _wire_tap_router(app)
    try:
        async with _SSEConnection(
            app, f"/v1/workspaces/{wid}/tap", cookie=cookie
        ) as conn:
            assert conn.status_code == 200
            await asyncio.sleep(0.05)
            # A new record appended AFTER connect.
            ws.append_record(sid, seq=3, kind="done")
            await bus.publish(f"session:{sid}:tick", {"seq": 3})

            # Only the post-connect record is delivered; seq 1,2 skipped.
            frames = await conn.read_frames(count=1)
            assert [f.data["class"] for f in frames] == ["done"]
            assert frames[0].data["seq"] == 3
    finally:
        await router.aclose()
        await bus.aclose()


@pytest.mark.asyncio
async def test_tap_requires_auth(app, workspace_registry):
    wid = "w-auth"
    await _ensure_workspace(app, workspace_registry, wid)
    bus, router = await _wire_tap_router(app)
    try:
        # No cookie → 401 from the require_auth gate / handler check.
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/v1/workspaces/{wid}/tap")
            assert resp.status_code == 401
    finally:
        await router.aclose()
        await bus.aclose()
