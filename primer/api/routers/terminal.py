"""Workspace integrated-terminal WebSocket (Studio spec §6.5).

``WS /v1/workspaces/{workspace_id}/terminal?cols=&rows=`` — a bidirectional
terminal channel bridging a browser (xterm.js, a later task) to a PTY.

Frame protocol (spec §6.5)
--------------------------
* **Binary** WS frames carry raw bytes:
  - client → server = pty stdin
  - server → client = pty output
* **Text** WS frames carry JSON control:
  - client → server: ``{"resize": {"cols": N, "rows": M}}``
  - server → client: ``{"exit": <code>}`` (sent once, then the socket closes)

Backends
--------
* **Local workspaces** — the PTY is hosted in-process here
  (:class:`~primer.workspace.local.pty_host.LocalPtySession`), cwd = the
  workspace root.
* **Container / K8s workspaces** — the endpoint proxies to the in-container
  runtime's PTY op via
  :meth:`~primer.workspace.runtime.ws_sandbox.WSSandbox.open_pty`.

Availability (spec §6.5, auth plan Task 8): the terminal is a real shell
inside the workspace sandbox, so it is **admin-only by default**. A
non-admin is admitted only when an operator flips the per-workspace
``terminal_user_access`` toggle (and the caller holds at least the ``user``
role); ``restricted`` accounts never get a shell. Access is auth-gated
(``require_auth_ws`` → close 4401) then role-gated (close 4403); the
workspace is the sandbox boundary. A workspace kind that supports neither
PTY path is closed with policy code 1011.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from fastapi import APIRouter, Query, WebSocket

from primer.api.deps import require_auth_ws, require_role_ws, require_user_ws
from primer.model.except_ import NotFoundError

logger = logging.getLogger(__name__)

terminal_router = APIRouter(tags=["workspace-terminal"])


# ---------------------------------------------------------------------------
# Uniform PTY session surface (local host + runtime proxy both satisfy it)
# ---------------------------------------------------------------------------


class _TerminalPty(Protocol):
    """The shape the endpoint drives — implemented by the local host and the
    runtime-proxy adapter alike."""

    async def start(self) -> None: ...
    def output(self) -> AsyncIterator[bytes]: ...
    async def write(self, data: bytes) -> None: ...
    async def resize(self, cols: int, rows: int) -> None: ...
    async def close(self) -> None: ...
    @property
    def exit_code(self) -> int | None: ...


class _RuntimePtyAdapter:
    """Adapt a :class:`RuntimePtyHandle` (container proxy) to :class:`_TerminalPty`."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self._exit_code: int | None = None

    async def start(self) -> None:
        # The handle is already open (open_pty was awaited during resolve).
        return None

    async def output(self) -> AsyncIterator[bytes]:
        # KNOWN FRAGILITY (BE10c): a transient runtime WS drop ends the
        # container terminal, and it is NOT resumable in v1.
        #
        # On any runtime WebSocket drop, RuntimeClient._on_disconnect ->
        # _close_all_streams pushes a stream-closed sentinel into every open
        # PTY stream (runtime_client.py). ``self._handle.events()`` then ends
        # WITHOUT ever yielding an "exit" frame, so ``_exit_code`` stays None.
        # The endpoint's _send_loop consequently reports ``{"exit": -1}`` and
        # closes the browser socket. A brief runtime blip therefore drops the
        # whole terminal even though the runtime may reconnect moments later.
        #
        # This is an accepted limitation for v1: the terminal is not reattached
        # across a runtime reconnect (there is no PTY session-resume protocol).
        # The reconnect loop keeps the RuntimeClient itself healthy; only this
        # one terminal stream is lost, and the user reopens the terminal.
        async for frame in self._handle.events():
            if frame.kind == "data":
                yield frame.data
            elif frame.kind == "exit":
                self._exit_code = frame.code
                return

    async def write(self, data: bytes) -> None:
        await self._handle.stdin(data)

    async def resize(self, cols: int, rows: int) -> None:
        await self._handle.resize(cols, rows)

    async def close(self) -> None:
        await self._handle.close()

    @property
    def exit_code(self) -> int | None:
        return self._exit_code


async def _default_resolve_pty(
    workspace: Any, *, cols: int, rows: int,
) -> _TerminalPty | None:
    """Pick the PTY host for *workspace*, or ``None`` if unsupported.

    Discriminator: a :class:`LocalWorkspace` (exposes a ``root: Path``) is
    hosted in-process; a runtime-backed workspace exposes a ``sandbox`` with
    an ``open_pty`` coroutine and is proxied. Anything else is unsupported.
    """
    from primer.workspace.local.pty_host import LocalPtySession
    from primer.workspace.local.workspace import LocalWorkspace

    if isinstance(workspace, LocalWorkspace):
        return LocalPtySession(
            root=workspace.root,
            cols=cols,
            rows=rows,
            env=getattr(workspace, "_env", None),
        )

    sandbox = getattr(workspace, "sandbox", None)
    open_pty = getattr(sandbox, "open_pty", None)
    if open_pty is not None:
        handle = await open_pty(cols=cols, rows=rows)
        return _RuntimePtyAdapter(handle)

    return None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@terminal_router.websocket("/workspaces/{workspace_id}/terminal")
async def workspace_terminal_ws(
    websocket: WebSocket,
    workspace_id: str,
    cols: int = Query(80, ge=1, le=1000),
    rows: int = Query(24, ge=1, le=1000),
) -> None:
    """Bidirectional terminal stream for a workspace (spec §6.5)."""
    # Auth: middleware populates websocket.state.user from the session
    # cookie. Close 4401 (mirrors the chat WS) when absent.
    if require_auth_ws(websocket) is None:
        await websocket.accept()
        await websocket.close(code=4401, reason="auth_required")
        return

    registry = getattr(websocket.app.state, "workspace_registry", None)
    if registry is None:
        await websocket.accept()
        await websocket.close(code=1011, reason="workspace_registry_unavailable")
        return

    # Resolve the live workspace handle (404-close on missing/destroyed).
    try:
        workspace = await registry.get_workspace(workspace_id)
    except NotFoundError:
        await websocket.accept()
        await websocket.close(code=4404, reason="workspace_not_found")
        return

    # RBAC role gate (auth plan Task 8): the integrated terminal is a real
    # shell inside the workspace sandbox, so it is admin-only by default. A
    # non-admin is admitted ONLY when an operator has flipped the
    # per-workspace ``terminal_user_access`` toggle AND the caller holds at
    # least the ``user`` role (``restricted`` never gets a shell). Otherwise
    # close 4403 (mirrors the 4401 auth close above). The toggle lives on the
    # resolved workspace handle, so this gate runs after get_workspace.
    if require_role_ws(websocket, "admin") is None:
        toggled = bool(getattr(workspace, "terminal_user_access", False))
        if not (toggled and require_user_ws(websocket) is not None):
            await websocket.accept()
            await websocket.close(code=4403, reason="forbidden_role")
            return

    resolver = getattr(
        websocket.app.state, "terminal_pty_resolver", _default_resolve_pty,
    )
    try:
        session = await resolver(workspace, cols=cols, rows=rows)
    except Exception:  # noqa: BLE001 — resolve failure must not 500 the socket
        logger.exception("terminal: failed to open PTY for %s", workspace_id)
        await websocket.accept()
        await websocket.close(code=1011, reason="terminal_open_failed")
        return

    if session is None:
        # Workspace kind supports neither a local nor a runtime PTY.
        await websocket.accept()
        await websocket.close(code=1011, reason="terminal_unsupported")
        return

    await websocket.accept()
    try:
        await session.start()
    except Exception:  # noqa: BLE001
        logger.exception("terminal: PTY start failed for %s", workspace_id)
        await websocket.close(code=1011, reason="terminal_start_failed")
        await session.close()
        return

    recv_task = asyncio.ensure_future(_recv_loop(websocket, session))
    send_task = asyncio.ensure_future(_send_loop(websocket, session))
    try:
        done, pending = await asyncio.wait(
            [recv_task, send_task], return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for task in done:
            # Consume the finished task's exception (e.g. the runtime
            # rejected pty_open) so it never logs as "never retrieved".
            if not task.cancelled():
                task.exception()
    finally:
        # Always tear the PTY down on disconnect (child terminated, fd freed).
        await session.close()


async def _recv_loop(websocket: WebSocket, session: _TerminalPty) -> None:
    """Browser → PTY: binary frames = stdin; text frames = JSON control."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            await session.write(data)
            continue
        text = message.get("text")
        if text is None:
            continue
        try:
            control = json.loads(text)
        except (ValueError, TypeError):
            continue
        resize = control.get("resize") if isinstance(control, dict) else None
        if isinstance(resize, dict):
            try:
                await session.resize(int(resize["cols"]), int(resize["rows"]))
            except (KeyError, TypeError, ValueError):
                continue


async def _send_loop(websocket: WebSocket, session: _TerminalPty) -> None:
    """PTY → browser: output bytes as binary frames; ``{"exit": code}`` at end."""
    async for chunk in session.output():
        await websocket.send_bytes(chunk)
    code = session.exit_code if session.exit_code is not None else -1
    try:
        await websocket.send_json({"exit": code})
        await websocket.close()
    except Exception:  # noqa: BLE001 — socket may already be gone
        pass


__all__ = ["terminal_router"]
