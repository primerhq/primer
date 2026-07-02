"""Tests for the workspace integrated-terminal WebSocket (Studio spec §6.5).

The endpoint (``WS /v1/workspaces/{wid}/terminal``) is exercised with a
REAL :class:`LocalPtySession` injected via the ``app.state.terminal_pty_resolver``
seam — that keeps the frame-protocol + auth + teardown plumbing honest while
decoupling from heavy ``LocalWorkspace`` materialisation (the workspace
registry is a small stub, since the endpoint only calls ``get_workspace``).
The local-vs-runtime discriminator (``_default_resolve_pty``) is covered by a
separate direct unit test.

PTYs are a POSIX/Linux facility; the socket tests are skipped elsewhere.
"""

from __future__ import annotations

import sys

import pytest
from starlette.testclient import TestClient as SyncTestClient
from starlette.websockets import WebSocketDisconnect

from primer.api.app import create_test_app
from primer.model.except_ import NotFoundError

_LINUX = sys.platform == "linux" and hasattr(__import__("os"), "openpty")


class _StubRegistry:
    """Minimal stand-in exposing only the ``get_workspace`` hook the
    terminal endpoint calls."""

    def __init__(self, *, exists: bool = True) -> None:
        self._exists = exists

    async def get_workspace(self, workspace_id: str) -> object:
        if not self._exists:
            raise NotFoundError(f"workspace {workspace_id!r} not found")
        return object()


def _build_app(sp, pr, *, exists: bool = True, root: str | None = None):
    app = create_test_app(storage_provider=sp, provider_registry=pr)
    app.state.workspace_registry = _StubRegistry(exists=exists)

    async def _resolver(workspace, *, cols: int, rows: int):
        from primer.workspace.local.pty_host import LocalPtySession
        return LocalPtySession(root=root, cols=cols, rows=rows, cmd=["/bin/sh"])

    app.state.terminal_pty_resolver = _resolver
    return app


def _login(sclient: SyncTestClient) -> None:
    sclient.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
    sclient.post("/v1/auth/login", json={"username": "testuser", "password": "testpassword"})


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
def test_local_terminal_echo(fake_storage_provider, fake_provider_registry, tmp_path):
    app = _build_app(fake_storage_provider, fake_provider_registry, root=str(tmp_path))
    with SyncTestClient(app) as sclient:
        _login(sclient)
        with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
            ws.send_bytes(b"echo hi\n")
            collected = b""
            for _ in range(50):
                collected += ws.receive_bytes()
                if b"hi" in collected:
                    break
            assert b"hi" in collected


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
def test_resize_control_frame_accepted(fake_storage_provider, fake_provider_registry, tmp_path):
    app = _build_app(fake_storage_provider, fake_provider_registry, root=str(tmp_path))
    with SyncTestClient(app) as sclient:
        _login(sclient)
        with sclient.websocket_connect("/v1/workspaces/ws-1/terminal?cols=80&rows=24") as ws:
            # A JSON resize control frame must be accepted (no close), and the
            # session keeps working afterwards.
            ws.send_json({"resize": {"cols": 120, "rows": 40}})
            ws.send_bytes(b"echo ok\n")
            collected = b""
            for _ in range(50):
                collected += ws.receive_bytes()
                if b"ok" in collected:
                    break
            assert b"ok" in collected


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
def test_unauthenticated_closes_4401(fake_storage_provider, fake_provider_registry, tmp_path):
    app = _build_app(fake_storage_provider, fake_provider_registry, root=str(tmp_path))
    with SyncTestClient(app) as sclient:
        # No login → the handler closes with 4401 right after accept.
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 4401


@pytest.mark.skipif(not _LINUX, reason="PTY requires a POSIX/Linux pseudo-terminal")
def test_missing_workspace_closes_4404(fake_storage_provider, fake_provider_registry, tmp_path):
    app = _build_app(fake_storage_provider, fake_provider_registry, exists=False, root=str(tmp_path))
    with SyncTestClient(app) as sclient:
        _login(sclient)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect("/v1/workspaces/does-not-exist/terminal") as ws:
                ws.receive_bytes()
        assert excinfo.value.code == 4404


@pytest.mark.asyncio
async def test_default_resolver_discriminates(tmp_path):
    """``_default_resolve_pty`` picks local vs runtime vs unsupported."""
    from primer.api.routers.terminal import _default_resolve_pty, _RuntimePtyAdapter

    # Unknown workspace kind → unsupported.
    assert await _default_resolve_pty(object(), cols=80, rows=24) is None

    # A runtime-backed workspace (has sandbox.open_pty) → proxy adapter.
    class _FakeHandle:
        pass

    class _FakeSandbox:
        def __init__(self):
            self.calls: list[dict] = []

        async def open_pty(self, *, cols, rows):
            self.calls.append({"cols": cols, "rows": rows})
            return _FakeHandle()

    class _FakeRuntimeWorkspace:
        def __init__(self):
            self.sandbox = _FakeSandbox()

    wsx = _FakeRuntimeWorkspace()
    session = await _default_resolve_pty(wsx, cols=100, rows=30)
    assert isinstance(session, _RuntimePtyAdapter)
    assert wsx.sandbox.calls == [{"cols": 100, "rows": 30}]
