"""Access gates on the workspace REST surface (access-control spec 6.2, 9; plan P2 Task 2).

The house multi-identity pattern (tests/api/test_crud_owned.py): a minimal
app with the error handlers, a test-only actor middleware that turns the
``X-Test-User: <id>:<role>`` header into ``request.state.actor`` /
``.user`` (the same two sources ``person_from_request`` and the role gates
read), the real SQLite provider, and the workspace routers under test.
Workspaces are seeded the way the routes read them: direct storage writes.
The workspace registry is an in-memory fake backend (the gates and the
storage paths are what is under test, not the backend).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import SecretStr
from starlette.datastructures import State
from starlette.testclient import TestClient as SyncTestClient
from starlette.websockets import WebSocketDisconnect

from primer.api.errors import register_error_handlers
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.api.routers import terminal, tap, workspaces
from primer.model.access import Access
from primer.model.except_ import NotFoundError
from primer.model.principal import Principal
from primer.model.provider import SqliteConfig
from primer.model.storage import FieldRef, Op, Predicate, Value
from primer.model.user import User
from primer.model.workspace import (
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from primer.storage.sqlite import SqliteStorageProvider

ALICE, BOB, CAROL, ADMIN = "alice", "bob", "carol", "admin"
TPL = "tpl-1"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _as(uid: str, role: str = "user") -> dict[str, str]:
    return {"X-Test-User": f"{uid}:{role}"}


# ===========================================================================
# In-memory workspace handle + backend fakes (the registry's factory)
# ===========================================================================


class _FakeWorkspace:
    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self._runtime_meta = WorkspaceRuntimeMeta(
            url=f"ws://fake/{workspace_id}", token=SecretStr(f"tok-{workspace_id}"),
        )

    @property
    def id(self) -> str:
        return self.workspace_id

    @property
    def runtime_meta(self) -> WorkspaceRuntimeMeta:
        return self._runtime_meta

    async def list_sessions(self) -> list:
        return []

    async def get_session(self, session_id: str):
        return None

    async def aclose(self) -> None:
        return


class _FakeBackend:
    def __init__(self, _provider: Any) -> None:
        self._workspaces: dict[str, _FakeWorkspace] = {}

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        self._workspaces.clear()

    async def create(
        self, template: Any, *, overrides: Any = None, workspace_id: str | None = None,
        resolvers: Any = None,
    ) -> _FakeWorkspace:
        wid = workspace_id or f"ws-{len(self._workspaces) + 1}"
        ws = _FakeWorkspace(wid)
        self._workspaces[wid] = ws
        return ws

    async def get(self, workspace_id: str, *, template: Any = None) -> _FakeWorkspace:
        # Adopt the row's instance like a local backend adopting its
        # directory: a seeded row re-attaches to a fresh in-memory handle.
        ws = self._workspaces.get(workspace_id)
        if ws is None:
            ws = _FakeWorkspace(workspace_id)
            self._workspaces[workspace_id] = ws
        return ws

    async def list(self) -> list[str]:
        return list(self._workspaces)

    async def destroy(self, workspace_id: str) -> None:
        if workspace_id not in self._workspaces:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        await self._workspaces[workspace_id].aclose()
        del self._workspaces[workspace_id]


# ===========================================================================
# The minimal app: error handlers, actor middleware, the routers under test
# ===========================================================================


class _ActorMiddleware:
    """Turns ``X-Test-User: <id>:<role>`` into ``state.actor`` + ``state.user``.

    The production AuthMiddleware populates exactly those two sources on
    the cookie and bearer paths; this stands in for it so every request is
    decided by the same ``person_from_request`` / role gates the real routes
    run.
    """

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            raw = [v for k, v in scope["headers"] if k == b"x-test-user"]
            if raw:
                state = scope.get("state")
                if not isinstance(state, State):
                    state = State()
                    scope["state"] = state
                uid, _, role = raw[0].decode().partition(":")
                user = User(id=uid, username=uid, role=role, created_at=NOW)
                state.user = user
                state.actor = Principal(
                    type="user", id=uid, display=uid, role=role,
                    source="test", user_id=uid,
                )
        await self.app(scope, receive, send)


def _build_app(sp: Any, pr: Any, wsr: Any) -> FastAPI:
    app = FastAPI(title="workspace access routes (test)")
    register_error_handlers(app)
    app.state.storage_provider = sp
    app.state.provider_registry = pr
    app.state.workspace_registry = wsr
    app.state.scheduler = object()  # create's session seeding is best-effort
    app.state.claim_engine = None
    app.state.event_bus = object()
    app.add_middleware(_ActorMiddleware)
    # The access sub-resource mounts BEFORE the workspace routes (the
    # leader's order; same prefix and tier as the feature routers).
    app.include_router(workspaces.workspace_access_router, prefix="/v1")
    app.include_router(workspaces.workspace_router, prefix="/v1")
    app.include_router(workspaces.sessions_router, prefix="/v1")
    app.include_router(workspaces.files_router, prefix="/v1")
    app.include_router(workspaces.log_router, prefix="/v1")
    app.include_router(workspaces.yields_pending_router, prefix="/v1")
    app.include_router(workspaces.events_router, prefix="/v1")
    app.include_router(tap.tap_router, prefix="/v1")
    app.include_router(terminal.terminal_router, prefix="/v1")
    return app


class _Env:
    def __init__(self, client: httpx.AsyncClient, sp: Any) -> None:
        self.client = client
        self.sp = sp


@pytest.fixture
async def env(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "ws-routes.sqlite"))
    await sp.initialize()
    pr = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )
    wsr = WorkspaceRegistry(sp, factory=_FakeBackend)  # type: ignore[arg-type]
    app = _build_app(sp, pr, wsr)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield _Env(client=client, sp=sp)
    await sp.aclose()


async def _mk_workspace(
    sp: Any,
    ws_id: str,
    *,
    owners: list[str] | None = None,
    grants: dict[str, str] | None = None,
    public: bool = False,
    name: str | None = None,
    terminal_user_access: bool = False,
) -> Workspace:
    row = Workspace(
        id=ws_id,
        template_id=TPL,
        provider_id="local-1",
        created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token=SecretStr("stub-token")),
        name=name,
        terminal_user_access=terminal_user_access,
        access=Access(owners=owners or [ALICE], grants=grants or {}, public=public),
    )
    await sp.get_storage(Workspace).create(row)
    return row


async def _seed_base(env: _Env) -> None:
    """The provider + template every created/seeded workspace references."""
    await env.sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(access=Access(public=True), 
            id="local-1",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(root_path="/tmp/ws-access-routes"),
        )
    )
    await env.sp.get_storage(WorkspaceTemplate).create(
        # Public: creating from a template needs read on it (Phase 1); this test is about the workspace's own access.
        WorkspaceTemplate(id=TPL, description="dev workspace", provider_id="local-1", access=Access(public=True))
    )


async def _seed_users(env: _Env, *ids: str) -> None:
    """User rows the sharing routes may name (set_access refuses unknown ids)."""
    for uid in ids:
        await env.sp.get_storage(User).create(
            User(id=uid, username=uid, created_at=NOW)
        )


async def _seed_matrix(env: _Env) -> None:
    """ws-a owned by alice (bob: read), ws-b owned by carol, ws-c public."""
    await _seed_base(env)
    await _seed_users(env, ALICE, BOB, CAROL)
    await _mk_workspace(env.sp, "ws-a", owners=[ALICE], grants={BOB: "read"}, name="alpha")
    await _mk_workspace(env.sp, "ws-b", owners=[CAROL], name="beta")
    await _mk_workspace(env.sp, "ws-c", owners=[ALICE], public=True, name="gamma")


# ===========================================================================
# READ gates: stranger 404 (missing-row text), reader 200
# ===========================================================================


@pytest.mark.asyncio
async def test_get_workspace_a_stranger_answers_the_missing_row_text(env: _Env) -> None:
    await _seed_matrix(env)
    r = await env.client.get("/v1/workspaces/ws-b", headers=_as(CAROL))
    assert r.status_code == 200  # sanity: the owner reads her own row
    r = await env.client.get("/v1/workspaces/ws-b", headers=_as(BOB))
    assert r.status_code == 404
    assert r.json()["detail"] == "Workspace 'ws-b' does not exist"


@pytest.mark.asyncio
async def test_list_sessions_a_stranger_404_a_reader_200(env: _Env) -> None:
    await _seed_matrix(env)
    r = await env.client.get("/v1/workspaces/ws-a/sessions", headers=_as(BOB))
    assert r.status_code == 200
    assert r.json()["items"] == []
    r = await env.client.get("/v1/workspaces/ws-b/sessions", headers=_as(BOB))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_session_a_stranger_answers_404(env: _Env) -> None:
    await _seed_matrix(env)
    r = await env.client.get("/v1/workspaces/ws-b/sessions/sess-1", headers=_as(BOB))
    assert r.status_code == 404


# ===========================================================================
# FULL gate: stranger 404, reader 403, owner 200
# ===========================================================================


@pytest.mark.asyncio
async def test_rename_a_stranger_404_a_reader_403_the_owner_200(env: _Env) -> None:
    await _seed_matrix(env)
    url = "/v1/workspaces/ws-a"
    r = await env.client.patch(url, json={"name": "x"}, headers=_as(CAROL))
    assert r.status_code == 404
    r = await env.client.patch(url, json={"name": "x"}, headers=_as(BOB))
    assert r.status_code == 403
    assert "needs full access" in r.json()["detail"]
    r = await env.client.patch(url, json={"name": "renamed"}, headers=_as(ALICE))
    assert r.status_code == 200
    assert r.json()["name"] == "renamed"


@pytest.mark.asyncio
async def test_write_file_a_stranger_404_a_reader_403(env: _Env) -> None:
    await _seed_matrix(env)
    url = "/v1/workspaces/ws-a/files?path=n.txt"
    r = await env.client.put(url, json={"content": "hi"}, headers=_as(CAROL))
    assert r.status_code == 404
    r = await env.client.put(url, json={"content": "hi"}, headers=_as(BOB))
    assert r.status_code == 403


# ===========================================================================
# list / find: the readable set (admin: no filter)
# ===========================================================================


@pytest.mark.asyncio
async def test_list_workspaces_shows_only_the_readable_set(env: _Env) -> None:
    await _seed_matrix(env)
    r = await env.client.get("/v1/workspaces", headers=_as(BOB))
    assert r.status_code == 200
    assert {w["id"] for w in r.json()["items"]} == {"ws-a", "ws-c"}
    r = await env.client.get("/v1/workspaces", headers=_as(CAROL))
    assert {w["id"] for w in r.json()["items"]} == {"ws-b", "ws-c"}
    r = await env.client.get("/v1/workspaces", headers=_as(ALICE))
    assert {w["id"] for w in r.json()["items"]} == {"ws-a", "ws-c"}
    r = await env.client.get("/v1/workspaces", headers=_as(ADMIN, "admin"))
    assert {w["id"] for w in r.json()["items"]} == {"ws-a", "ws-b", "ws-c"}


@pytest.mark.asyncio
async def test_list_workspaces_with_no_readable_rows_is_empty(env: _Env) -> None:
    await _seed_matrix(env)
    # A restricted account holds no level on any row (the onboarding gate):
    # its readable set is empty, and the list answers an empty page.
    r = await env.client.get("/v1/workspaces", headers=_as("dora", "restricted"))
    assert r.status_code == 200
    assert r.json()["items"] == []
    # a plain stranger reads exactly the public rows
    r = await env.client.get("/v1/workspaces", headers=_as("dora"))
    assert {w["id"] for w in r.json()["items"]} == {"ws-c"}


@pytest.mark.asyncio
async def test_find_workspaces_andes_the_predicate_with_the_readable_set(
    env: _Env,
) -> None:
    await _seed_matrix(env)
    # name == "beta" matches only ws-b: bob must see nothing, carol exactly it.
    predicate = Predicate(
        left=FieldRef(name="name"), op=Op.EQ, right=Value(value="beta"),
    ).model_dump()
    body = {"predicate": predicate, "page": {"kind": "offset", "offset": 0, "length": 50}}
    r = await env.client.post("/v1/workspaces/find", json=body, headers=_as(BOB))
    assert r.status_code == 200
    assert [w["id"] for w in r.json()["items"]] == []
    r = await env.client.post("/v1/workspaces/find", json=body, headers=_as(CAROL))
    assert [w["id"] for w in r.json()["items"]] == ["ws-b"]


# ===========================================================================
# create stamps the caller as the owner (body access is ignored)
# ===========================================================================


@pytest.mark.asyncio
async def test_create_stamps_the_caller_as_owner_and_ignores_body_access(
    env: _Env,
) -> None:
    await _seed_base(env)
    # bob creates; the body tries to make alice the owner instead.
    r = await env.client.post(
        "/v1/workspaces",
        json={
            "template_id": TPL,
            "id": "bob-ws",
            "access": {"owners": [ALICE]},
        },
        headers=_as(BOB),
    )
    assert r.status_code == 201
    row = await env.sp.get_storage(Workspace).get("bob-ws")
    assert row.access.owners == [BOB]
    # the creator reads the new workspace; a stranger cannot
    r = await env.client.get("/v1/workspaces/bob-ws", headers=_as(BOB))
    assert r.status_code == 200
    r = await env.client.get("/v1/workspaces/bob-ws", headers=_as(ALICE))
    assert r.status_code == 404


# ===========================================================================
# the access sub-resource (GET/PUT /workspaces/{id}/access)
# ===========================================================================


@pytest.mark.asyncio
async def test_access_sub_resource_shares_the_workspace(env: _Env) -> None:
    await _seed_matrix(env)
    # a stranger sees nothing (not even that sharing exists)
    r = await env.client.get("/v1/workspaces/ws-b/access", headers=_as(BOB))
    assert r.status_code == 404
    # a reader sees the sharing
    r = await env.client.get("/v1/workspaces/ws-a/access", headers=_as(BOB))
    assert r.status_code == 200
    # only the owner may re-share; the reader is refused
    r = await env.client.put(
        "/v1/workspaces/ws-a/access",
        json={"owners": [ALICE], "grants": {BOB: "full"}},
        headers=_as(BOB),
    )
    assert r.status_code == 403
    r = await env.client.put(
        "/v1/workspaces/ws-a/access",
        json={"owners": [ALICE], "grants": {BOB: "full"}},
        headers=_as(ALICE),
    )
    assert r.status_code == 200
    # bob is now full on ws-a: the FULL gate admits him
    r = await env.client.patch(
        "/v1/workspaces/ws-a", json={"name": "z"}, headers=_as(BOB)
    )
    assert r.status_code == 200


# ===========================================================================
# delete needs the owner (the rank scan is gone; the reserved rule stays)
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_requires_the_owner(env: _Env) -> None:
    await _seed_matrix(env)
    r = await env.client.delete("/v1/workspaces/ws-b", headers=_as(BOB))
    assert r.status_code == 404
    r = await env.client.delete("/v1/workspaces/ws-a", headers=_as(BOB))
    assert r.status_code == 403
    # the owner deletes (the fake backend needs the live instance: create it
    # through the route first, which also exercises the owner stamp)
    r = await env.client.post(
        "/v1/workspaces",
        json={"template_id": TPL, "id": "own-ws"},
        headers=_as(ALICE),
    )
    assert r.status_code == 201
    r = await env.client.delete("/v1/workspaces/own-ws", headers=_as(ALICE))
    assert r.status_code == 204


# ===========================================================================
# terminal_access: owner or admin (used to be admin-only)
# ===========================================================================


@pytest.mark.asyncio
async def test_terminal_access_owner_or_admin(env: _Env) -> None:
    await _seed_matrix(env)
    url = "/v1/workspaces/ws-a/terminal_access"
    r = await env.client.put(url, json={"enabled": True}, headers=_as(CAROL))
    assert r.status_code == 404
    r = await env.client.put(url, json={"enabled": True}, headers=_as(BOB))
    assert r.status_code == 403
    r = await env.client.put(url, json={"enabled": True}, headers=_as(ALICE))
    assert r.status_code == 200
    r = await env.client.get("/v1/workspaces/ws-a", headers=_as(ALICE))
    assert r.json()["terminal_user_access"] is True
    # an admin may flip it too
    r = await env.client.put(url, json={"enabled": False}, headers=_as(ADMIN, "admin"))
    assert r.status_code == 200


# ===========================================================================
# the tap stream refuses at connect
# ===========================================================================


@pytest.mark.asyncio
async def test_tap_a_stranger_is_refused_and_a_reader_passes_the_gate(
    env: _Env,
) -> None:
    await _seed_matrix(env)
    r = await env.client.get("/v1/workspaces/ws-b/tap", headers=_as(BOB))
    assert r.status_code == 404
    # bob passes the gate: the minimal app has no tap router configured, so
    # the route answers 503 subsystem-inactive (not 403/404) once admitted.
    r = await env.client.get("/v1/workspaces/ws-a/tap", headers=_as(BOB))
    assert r.status_code in (200, 503)


# ===========================================================================
# the terminal WebSocket refuses at connect (full, not just the role flag)
# ===========================================================================


class _FakePty:
    """Admits the caller by emitting one frame (a refusal closes instead, so
    the probes can tell admission from refusal by what ``receive`` returns)."""

    async def start(self) -> None:
        return

    async def output(self) -> Any:
        yield b"ADM\n"
        await asyncio.sleep(3600)

    async def write(self, data: bytes) -> None:
        return

    async def resize(self, cols: int, rows: int) -> None:
        return

    async def close(self) -> None:
        return

    @property
    def exit_code(self) -> int | None:
        return None


class _StubRegistry:
    """The two hooks the terminal connect path calls (test_terminal's shape)."""

    def __init__(self, handle: _FakeWorkspace, terminal_user_access: bool) -> None:
        self._handle = handle
        self._terminal_user_access = terminal_user_access

    async def get_workspace(self, workspace_id: str) -> object:
        if workspace_id != self._handle.workspace_id:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        return self._handle

    async def get_workspace_row(self, workspace_id: str) -> Any:
        from types import SimpleNamespace

        if workspace_id != self._handle.workspace_id:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        return SimpleNamespace(terminal_user_access=self._terminal_user_access)


class _MemStorage:
    def __init__(self, rows: dict) -> None:
        self._rows = rows

    async def get(self, row_id: str):
        return self._rows.get(row_id)

    async def create(self, row: Any):
        self._rows[row.id] = row
        return row


class _MemSP:
    """The one storage the terminal connect path reads: workspace rows."""

    def __init__(self, workspaces: dict) -> None:
        self._stores: dict[Any, _MemStorage] = {Workspace: _MemStorage(workspaces)}

    def get_storage(self, cls: Any) -> _MemStorage:
        return self._stores[cls]

    async def initialize(self) -> None:
        return

    async def aclose(self) -> None:
        return


def test_terminal_ws_refusals_at_connect() -> None:
    """Stranger: the missing-workspace close. Reader (flag on): 4403 (FULL is
    needed, not just the role + flag). Owner (flag on): admitted."""
    ws_a = _FakeWorkspace("ws-a")
    rows = {
        "ws-a": Workspace(
            id="ws-a",
            template_id=TPL,
            provider_id="local-1",
            created_at=NOW,
            runtime_meta=ws_a.runtime_meta,
            terminal_user_access=True,
            access=Access(owners=[ALICE], grants={BOB: "read"}),
        )
    }
    sp = _MemSP(rows)
    app = FastAPI(title="terminal ws access (test)")
    register_error_handlers(app)
    app.state.storage_provider = sp
    app.state.provider_registry = object()
    app.state.workspace_registry = _StubRegistry(ws_a, terminal_user_access=True)
    app.add_middleware(_ActorMiddleware)

    async def _resolve(_workspace: Any, *, cols: int, rows: int) -> _FakePty:
        return _FakePty()

    app.state.terminal_pty_resolver = _resolve
    app.include_router(terminal.terminal_router, prefix="/v1")

    with SyncTestClient(app) as sclient:
        # a stranger answers the missing-workspace close (before any data)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect(
                "/v1/workspaces/ws-a/terminal", headers=_as(CAROL)
            ) as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 4404

        # a reader (even with the terminal flag on) needs FULL: refused.
        # (Without the gate the fake PTY admits and ``receive_bytes`` returns
        # the ADM frame, so this probe fails without hanging.)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect(
                "/v1/workspaces/ws-a/terminal", headers=_as(BOB)
            ) as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 4403

        # the owner with the flag on passes the gate (the fake PTY admits)
        with sclient.websocket_connect(
            "/v1/workspaces/ws-a/terminal", headers=_as(ALICE)
        ) as ws:
            assert ws.receive_bytes() == b"ADM\n"
            ws.close()


class _RecordingPty(_FakePty):
    """Admits with one frame, then echoes every write (so a test can tell a keystroke that reached the shell)."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._echo: asyncio.Queue[bytes] = asyncio.Queue()

    async def output(self) -> Any:
        yield b"ADM\n"
        while True:
            yield await self._echo.get()

    async def write(self, data: bytes) -> None:
        self.written.append(data)
        self._echo.put_nowait(data)


@pytest.mark.parametrize("after, code", [({}, 4404), ({BOB: "read"}, 4403)])
def test_terminal_ws_closes_at_the_first_keystroke_after_a_revoke(after: dict, code: int) -> None:
    """Final access review F8: the terminal re-checks FULL on each input, so a shell opened before a revoke (or a downgrade to read) closes
    with the code a fresh connect would get, and that keystroke never reaches the PTY."""
    ws_a = _FakeWorkspace("ws-a")
    row = Workspace(
        id="ws-a", template_id=TPL, provider_id="local-1", created_at=NOW, runtime_meta=ws_a.runtime_meta,
        terminal_user_access=True, access=Access(owners=[ALICE], grants={BOB: "full"}, rev=1),
    )
    rows = {"ws-a": row}
    app = FastAPI(title="terminal ws revoke (test)")
    register_error_handlers(app)
    app.state.storage_provider = _MemSP(rows)
    app.state.provider_registry = object()
    app.state.workspace_registry = _StubRegistry(ws_a, terminal_user_access=True)
    app.add_middleware(_ActorMiddleware)
    pty = _RecordingPty()

    async def _resolve(_workspace: Any, *, cols: int, rows: int) -> _RecordingPty:
        return pty

    app.state.terminal_pty_resolver = _resolve
    app.include_router(terminal.terminal_router, prefix="/v1")

    with SyncTestClient(app) as sclient:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect("/v1/workspaces/ws-a/terminal", headers=_as(BOB)) as ws:
                assert ws.receive_bytes() == b"ADM\n"
                ws.send_bytes(b"ls\n")
                assert ws.receive_bytes() == b"ls\n"
                rows["ws-a"] = row.model_copy(update={"access": Access(owners=[ALICE], grants=after, rev=2)})
                ws.send_bytes(b"cat /secret\n")
                ws.receive_bytes()
        assert excinfo.value.code == code
    assert pty.written == [b"ls\n"]
