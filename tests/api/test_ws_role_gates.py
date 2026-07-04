"""RBAC role gates on the streaming handlers (auth plan Task 8).

Covers the in-handler role checks added to the chat WebSocket, the
workspace terminal WebSocket, and the workspace tap SSE stream:

* chat WS  — requires role in {"user", "admin"}; ``restricted`` → close 4403.
* terminal — admin-only by default; a non-admin is admitted only when the
  per-workspace ``terminal_user_access`` toggle is on; else → close 4403.
* tap SSE  — requires role in {"user", "admin"}; ``restricted`` → 403.

These exercise the real app (``create_test_app``) through Starlette's
synchronous ``TestClient`` because httpx's ASGI client cannot drive
WebSocket upgrades. Role-specific users are seeded directly into the
in-memory store (the register endpoint only ever mints the FIRST user as
admin), then logged in over HTTP to obtain the signed ``primer_session``
cookie.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient as SyncTestClient
from starlette.websockets import WebSocketDisconnect

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.api.app import create_test_app
from primer.auth.passwords import hash_password
from primer.model.user import User

_LINUX = sys.platform == "linux" and hasattr(__import__("os"), "openpty")


def _seed_user(sp, *, uid: str, username: str, password: str, role: str) -> None:
    """Create a User row with an explicit role in the in-memory store.

    Runs on a throwaway event loop before the SyncTestClient starts its
    own — the fake storage is not loop-bound (the same cross-loop seed
    pattern the envelope test uses via its async fixture).
    """

    async def _mk() -> None:
        await sp.get_storage(User).create(
            User(
                id=uid,
                username=username,
                password_hash=await hash_password(password),
                created_at=datetime.now(timezone.utc),
                role=role,
            )
        )

    asyncio.run(_mk())


def _login(sclient: SyncTestClient, username: str, password: str) -> None:
    resp = sclient.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# chat WS
# ---------------------------------------------------------------------------


def test_chat_ws_restricted_closes_4403(
    fake_storage_provider, fake_provider_registry
):
    """A role='restricted' account is authenticated but may NOT open a chat
    socket: the handler closes 4403 before it ever resolves the chat row."""
    _seed_user(
        fake_storage_provider,
        uid="user-r",
        username="restr",
        password="pw-restr-123",
        role="restricted",
    )
    app_ = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )
    with SyncTestClient(app_) as sclient:
        _login(sclient, "restr", "pw-restr-123")
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect("/v1/chats/does-not-exist/ws") as ws:
                ws.receive_json()
    assert excinfo.value.code == 4403


def test_chat_ws_user_passes_role_gate(
    fake_storage_provider, fake_provider_registry
):
    """A role='user' account clears the role gate; with a missing chat the
    handler then closes 4404 — proving the gate ADMITTED the user (a 4403
    would mean it rejected). 4404 != 4403 is the assertion."""
    _seed_user(
        fake_storage_provider,
        uid="user-u",
        username="plain",
        password="pw-plain-123",
        role="user",
    )
    app_ = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )
    with SyncTestClient(app_) as sclient:
        _login(sclient, "plain", "pw-plain-123")
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect("/v1/chats/does-not-exist/ws") as ws:
                ws.receive_json()
    assert excinfo.value.code == 4404


# ---------------------------------------------------------------------------
# terminal WS
# ---------------------------------------------------------------------------


class _StubWorkspace:
    """Live-workspace stand-in carrying only the per-workspace terminal
    enable toggle the role gate reads."""

    def __init__(self, *, terminal_user_access: bool = False) -> None:
        self.terminal_user_access = terminal_user_access


class _StubRegistry:
    def __init__(self, workspace: object) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str) -> object:
        return self._workspace


def _build_terminal_app(sp, pr, *, toggle: bool, root: str | None = None):
    app_ = create_test_app(storage_provider=sp, provider_registry=pr)
    app_.state.workspace_registry = _StubRegistry(
        _StubWorkspace(terminal_user_access=toggle)
    )

    async def _resolver(workspace, *, cols: int, rows: int):
        from primer.workspace.local.pty_host import LocalPtySession

        return LocalPtySession(root=root, cols=cols, rows=rows, cmd=["/bin/sh"])

    app_.state.terminal_pty_resolver = _resolver
    return app_


def test_terminal_non_admin_without_toggle_closes_4403(
    fake_storage_provider, fake_provider_registry
):
    """Admin-only by default: a role='user' account with the toggle OFF is
    closed 4403 before any PTY is opened (cross-platform — no PTY reached)."""
    _seed_user(
        fake_storage_provider,
        uid="user-u",
        username="plain",
        password="pw-plain-123",
        role="user",
    )
    app_ = _build_terminal_app(
        fake_storage_provider, fake_provider_registry, toggle=False
    )
    with SyncTestClient(app_) as sclient:
        _login(sclient, "plain", "pw-plain-123")
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
                ws.receive_bytes()
    assert excinfo.value.code == 4403


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
def test_terminal_non_admin_with_toggle_admitted(
    fake_storage_provider, fake_provider_registry, tmp_path
):
    """Toggle ON → the role='user' account is admitted and the shell echoes."""
    _seed_user(
        fake_storage_provider,
        uid="user-u",
        username="plain",
        password="pw-plain-123",
        role="user",
    )
    app_ = _build_terminal_app(
        fake_storage_provider,
        fake_provider_registry,
        toggle=True,
        root=str(tmp_path),
    )
    with SyncTestClient(app_) as sclient:
        _login(sclient, "plain", "pw-plain-123")
        with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
            ws.send_bytes(b"echo hi\n")
            collected = b""
            for _ in range(50):
                collected += ws.receive_bytes()
                if b"hi" in collected:
                    break
            assert b"hi" in collected


# ---------------------------------------------------------------------------
# tap SSE (HTTP analogue of the WS close)
# ---------------------------------------------------------------------------


def test_tap_restricted_returns_403(
    fake_storage_provider, fake_provider_registry
):
    """A role='restricted' account is rejected from the tap stream with the
    HTTP analogue of the WS 4403 close: 403 forbidden_role (before the
    workspace is ever resolved)."""
    _seed_user(
        fake_storage_provider,
        uid="user-r",
        username="restr",
        password="pw-restr-123",
        role="restricted",
    )
    app_ = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )
    with SyncTestClient(app_) as sclient:
        _login(sclient, "restr", "pw-restr-123")
        resp = sclient.get("/v1/workspaces/ws-1/tap")
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "forbidden_role"
