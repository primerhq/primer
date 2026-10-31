"""The terminal route opens the PTY only once the socket is accepted, inside the block that always closes it (review W55k).

The route opened the PTY (for a container workspace: a ``pty_open`` on the runtime, with its stream) BEFORE ``accept()`` and
outside the ``try/finally`` that closes it, so a client that went away before the accept left a shell running in the
runtime and a stream slot in the client. The close codes a client sees are unchanged.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient as SyncTestClient
from starlette.websockets import WebSocketDisconnect

from primer.api.app import create_test_app
from primer.api.routers.terminal import workspace_terminal_ws
from primer.model.user import User
from tests.api.test_terminal import _login, _StubRegistry

_LINUX = sys.platform == "linux" and hasattr(__import__("os"), "openpty")


class _Pty:
    """A PTY session that records what happened to it."""

    def __init__(self, *, start_error: Exception | None = None) -> None:
        self.start_error = start_error
        self.started = self.closed = 0
        self.exit_code = None

    async def start(self) -> None:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    async def output(self):
        if False:  # pragma: no cover - an empty async iterator
            yield b""

    async def write(self, data: bytes) -> None:
        return None

    async def resize(self, cols: int, rows: int) -> None:
        return None

    async def close(self) -> None:
        self.closed += 1


def _app(fake_storage_provider, fake_provider_registry, resolver):
    app = create_test_app(storage_provider=fake_storage_provider, provider_registry=fake_provider_registry)
    app.state.workspace_registry = _StubRegistry()
    app.state.terminal_pty_resolver = resolver
    return app


def _close_of(app) -> tuple[int, str]:
    with SyncTestClient(app) as sclient:
        _login(sclient)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
                ws.receive_bytes()
    return excinfo.value.code, excinfo.value.reason


# ---- characterization: the close codes a client sees -----------------------------------------------------------------------------


@pytest.mark.skipif(not _LINUX, reason="the terminal tests run where PTYs exist")
def test_a_pty_that_cannot_be_opened_closes_1011(fake_storage_provider, fake_provider_registry) -> None:
    async def _resolver(workspace, *, cols, rows):
        raise RuntimeError("pty_open refused")

    assert _close_of(_app(fake_storage_provider, fake_provider_registry, _resolver)) == (1011, "terminal_open_failed")


@pytest.mark.skipif(not _LINUX, reason="the terminal tests run where PTYs exist")
def test_a_workspace_without_a_pty_closes_1011(fake_storage_provider, fake_provider_registry) -> None:
    async def _resolver(workspace, *, cols, rows):
        return None

    assert _close_of(_app(fake_storage_provider, fake_provider_registry, _resolver)) == (1011, "terminal_unsupported")


@pytest.mark.skipif(not _LINUX, reason="the terminal tests run where PTYs exist")
def test_a_pty_that_cannot_start_closes_1011_and_is_closed(fake_storage_provider, fake_provider_registry) -> None:
    opened: list[_Pty] = []

    async def _resolver(workspace, *, cols, rows):
        opened.append(_Pty(start_error=OSError("no pty")))
        return opened[-1]

    assert _close_of(_app(fake_storage_provider, fake_provider_registry, _resolver)) == (1011, "terminal_start_failed")
    assert [(p.started, p.closed) for p in opened] == [(1, 1)]


@pytest.mark.skipif(not _LINUX, reason="the terminal tests run where PTYs exist")
def test_a_pty_that_ends_is_closed_and_reports_its_exit(fake_storage_provider, fake_provider_registry) -> None:
    opened: list[_Pty] = []

    async def _resolver(workspace, *, cols, rows):
        opened.append(_Pty())
        return opened[-1]

    app = _app(fake_storage_provider, fake_provider_registry, _resolver)
    with SyncTestClient(app) as sclient:
        _login(sclient)
        with sclient.websocket_connect("/v1/workspaces/ws-1/terminal") as ws:
            assert ws.receive_json() == {"exit": -1}
    assert [(p.started, p.closed) for p in opened] == [(1, 1)]


# ---- W55k: a client gone before the accept leaves no PTY behind ------------------------------------------------------------------


class _SocketGoneBeforeAccept:
    """An admin's terminal socket whose client disconnected before the server accepted it."""

    def __init__(self, resolver) -> None:
        admin = User(id="u-admin", username="admin", password_hash=None, created_at=datetime.now(timezone.utc), role="admin")
        self.state = SimpleNamespace(user=admin, actor=SimpleNamespace(type="user", user_id="u-admin", role="admin"))
        self.app = SimpleNamespace(state=SimpleNamespace(
            workspace_registry=_StubRegistry(), terminal_pty_resolver=resolver, storage_provider=None,
        ))
        self.closes: list[tuple[int, str | None]] = []

    async def accept(self) -> None:
        raise RuntimeError("the client went away")

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closes.append((code, reason))


async def test_a_client_gone_before_the_accept_leaves_no_pty_open() -> None:
    opened: list[_Pty] = []

    async def _resolver(workspace, *, cols, rows):
        opened.append(_Pty())
        return opened[-1]

    with pytest.raises(RuntimeError, match="went away"):
        await workspace_terminal_ws(_SocketGoneBeforeAccept(_resolver), "ws-1", cols=80, rows=24)

    assert all(p.closed for p in opened), "a PTY was opened for a socket that was never accepted, and nothing closed it"
