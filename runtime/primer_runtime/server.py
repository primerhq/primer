"""aiohttp WebSocket server for the workspace runtime.

Entry point: ``python -m primer_runtime.server``

Environment variables:
    PRIMER_RUNTIME_TOKEN  — required shared secret; auth every WS connection.
    WORKSPACE_ROOT        — path to write ``.runtime.ready``; default ``/workspace``.
    RUNTIME_HOST          — bind host; default ``0.0.0.0``.
    RUNTIME_PORT          — bind port; default ``5959``.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import pathlib
import tempfile

from aiohttp import web
from aiohttp import WSMsgType

from primer_runtime.exec import run_exec
from primer_runtime.ops import HANDLERS, OpError
from primer_runtime.protocol import ErrorCode, OpName, Response, serialize
from primer_runtime.pty_op import PtyRegistry, start_pty
from primer_runtime.watch import WatchRegistry, cancel_watch, start_watch

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Typed app keys (avoids aiohttp NotAppKeyWarning)
# ---------------------------------------------------------------------------

_KEY_TOKEN: web.AppKey[str] = web.AppKey("runtime_token", str)
_KEY_WORKSPACE_ROOT: web.AppKey[str] = web.AppKey("workspace_root", str)

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION: str = "1.1"
RUNTIME_VERSION: str = "1.0.0"

_PROTOCOL_MAJOR: int = int(PROTOCOL_VERSION.split(".")[0])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_token(expected: str, provided: str | None) -> bool:
    """Compare bearer tokens via hmac.compare_digest to prevent timing attacks."""
    if provided is None:
        return False
    return hmac.compare_digest(expected.encode(), provided.encode())


def _extract_bearer(request: web.Request) -> str | None:
    """Extract the token from ``Authorization: Bearer <token>``."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):]
    return None


def _write_ready_marker(workspace_root: str) -> None:
    """Atomically write /workspace/.runtime.ready via tmpfile + rename."""
    root = pathlib.Path(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    ready = root / ".runtime.ready"
    # Use tmp file in same directory for atomic rename
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".runtime.ready.")
    try:
        os.close(fd)
        os.replace(tmp, ready)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log.info("Ready marker written: %s", ready)


# ---------------------------------------------------------------------------
# WS handler
# ---------------------------------------------------------------------------


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Upgrade to WebSocket, authenticate, then handle protocol ops."""
    token: str = request.app[_KEY_TOKEN]
    provided = _extract_bearer(request)
    if not _check_token(token, provided):
        raise web.HTTPUnauthorized(reason="Invalid or missing bearer token")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # --- Handshake: first message must be op="hello" ----------------------
    msg = await ws.receive()
    if msg.type != WSMsgType.TEXT:
        await ws.close(code=4400, message=b"expected_text_frame")
        return ws

    try:
        data = msg.json()
    except Exception:
        await ws.close(code=4400, message=b"invalid_json")
        return ws

    op = data.get("op")
    if op != OpName.HELLO:
        await ws.close(code=4400, message=b"expected_hello")
        return ws

    args = data.get("args") or {}
    client_protocol: str = args.get("protocol", "")
    try:
        client_major = int(client_protocol.split(".")[0])
    except (ValueError, IndexError):
        await ws.close(code=4400, message=b"invalid_protocol_version")
        return ws

    if client_major != _PROTOCOL_MAJOR:
        await ws.close(code=4400, message=b"protocol_major_mismatch")
        return ws

    # Handshake OK
    req_id = data.get("req_id", 0)
    hello_resp = Response(
        req_id=req_id,
        ok=True,
        result={"protocol": PROTOCOL_VERSION, "runtime": RUNTIME_VERSION},
    )
    await ws.send_str(serialize(hello_resp))

    # --- Post-handshake message loop -----------------------------------------
    workspace_root: str = request.app[_KEY_WORKSPACE_ROOT]

    # Per-connection watch registry — tracks active subscription tasks.
    watch_registry = WatchRegistry()
    # Per-connection PTY registry — tracks live interactive terminal sessions.
    pty_registry = PtyRegistry()

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                frame = msg.json()
            except Exception:
                continue  # ignore malformed frames

            frame_req_id = frame.get("req_id", 0)
            op_name: str = frame.get("op", "")
            args: dict = frame.get("args") or {}

            # --- Watch ops -----------------------------------------------
            if op_name == OpName.WATCH_START:
                start_watch(
                    req_id=frame_req_id,
                    args=args,
                    workspace_root=workspace_root,
                    send=ws.send_str,
                    registry=watch_registry,
                )
                continue

            if op_name == OpName.WATCH_CANCEL:
                target_req_id: int = (args or {}).get("target_req_id", -1)
                cancel_watch(target_req_id, watch_registry)
                # watch_closed is emitted by the subscription task itself
                continue

            # --- Interactive PTY ops --------------------------------------
            # PTY_OPEN is a long-lived streaming op (emits pty_open/data/exit
            # keyed by its req_id); the others are single-shot control
            # requests carrying target_req_id (mirrors WATCH_START/CANCEL).
            if op_name == OpName.PTY_OPEN:
                start_pty(
                    req_id=frame_req_id,
                    args=args,
                    workspace_root=workspace_root,
                    send=ws.send_str,
                    registry=pty_registry,
                )
                continue

            if op_name in (OpName.PTY_STDIN, OpName.PTY_RESIZE, OpName.PTY_CLOSE):
                # Fully wrapped: these args come from client frames, and ANY
                # exception escaping here (e.g. int("abc") → ValueError;
                # struct.error pre-clamp) would break out of the message
                # loop, skipping the post-loop cancel_all() teardown and
                # leaking every PTY/watch on this connection.
                try:
                    pty_target: int = args.get("target_req_id", -1)
                    found = True
                    if op_name == OpName.PTY_STDIN:
                        try:
                            data = base64.b64decode(args.get("data_b64", "") or "")
                        except Exception:  # noqa: BLE001
                            await ws.send_str(serialize(Response(
                                req_id=frame_req_id, ok=False,
                                error={"code": ErrorCode.EPROTOCOL, "message": "pty_stdin: invalid base64"},
                            )))
                            continue
                        found = pty_registry.write_stdin(pty_target, data)
                    elif op_name == OpName.PTY_RESIZE:
                        found = pty_registry.resize(
                            pty_target,
                            int(args.get("cols") or 80),
                            int(args.get("rows") or 24),
                        )
                    else:  # PTY_CLOSE
                        found = pty_registry.close(pty_target)
                except Exception as exc:  # noqa: BLE001 — protocol boundary
                    await ws.send_str(serialize(Response(
                        req_id=frame_req_id, ok=False,
                        error={"code": ErrorCode.EPROTOCOL, "message": f"{op_name}: {exc}"},
                    )))
                    continue
                if found:
                    await ws.send_str(serialize(Response(req_id=frame_req_id, ok=True, result={"ok": True})))
                else:
                    await ws.send_str(serialize(Response(
                        req_id=frame_req_id, ok=False,
                        error={"code": ErrorCode.ENOENT, "message": f"no PTY for target_req_id={pty_target}"},
                    )))
                continue

            # --- Streaming exec op ----------------------------------------
            if op_name == OpName.EXEC:
                try:
                    async for event in run_exec(frame_req_id, args, workspace_root):
                        await ws.send_str(serialize(event))
                except OpError as exc:
                    err_resp = Response(
                        req_id=frame_req_id,
                        ok=False,
                        error={"code": exc.code, "message": exc.message},
                    )
                    await ws.send_str(serialize(err_resp))
                except Exception as exc:  # noqa: BLE001
                    log.exception("Unexpected error handling exec op")
                    err_resp = Response(
                        req_id=frame_req_id,
                        ok=False,
                        error={"code": ErrorCode.EINTERNAL, "message": str(exc)},
                    )
                    await ws.send_str(serialize(err_resp))
                continue

            # --- Single-shot ops ------------------------------------------
            handler = HANDLERS.get(op_name)
            if handler is None:
                err_resp = Response(
                    req_id=frame_req_id,
                    ok=False,
                    error={"code": ErrorCode.EUNSUPPORTED, "message": f"Op not implemented: {op_name!r}"},
                )
                await ws.send_str(serialize(err_resp))
                continue

            try:
                result = await handler(args, workspace_root)  # type: ignore[operator]
                ok_resp = Response(req_id=frame_req_id, ok=True, result=result)
                await ws.send_str(serialize(ok_resp))
            except OpError as exc:
                err_resp = Response(
                    req_id=frame_req_id,
                    ok=False,
                    error={"code": exc.code, "message": exc.message},
                )
                await ws.send_str(serialize(err_resp))
            except Exception as exc:  # noqa: BLE001
                log.exception("Unexpected error handling op %r", op_name)
                err_resp = Response(
                    req_id=frame_req_id,
                    ok=False,
                    error={"code": ErrorCode.EINTERNAL, "message": str(exc)},
                )
                await ws.send_str(serialize(err_resp))
        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
            break

    # Cancel any lingering watch subscriptions / PTY sessions on disconnect.
    watch_registry.cancel_all()
    pty_registry.cancel_all()

    return ws


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_app(*, token: str | None = None, workspace_root: str | None = None) -> web.Application:
    """Create and configure the aiohttp Application.

    Parameters
    ----------
    token:
        Override the ``PRIMER_RUNTIME_TOKEN`` env var (useful in tests).
    workspace_root:
        Override the ``WORKSPACE_ROOT`` env var (useful in tests).
    """
    resolved_token = token or os.environ.get("PRIMER_RUNTIME_TOKEN", "")
    if not resolved_token:
        raise RuntimeError("PRIMER_RUNTIME_TOKEN must be set (or pass token= to build_app)")

    resolved_root = workspace_root or os.environ.get("WORKSPACE_ROOT", "/workspace")

    app = web.Application()
    app[_KEY_TOKEN] = resolved_token
    app[_KEY_WORKSPACE_ROOT] = resolved_root

    # Write the ready marker on startup (inside the event loop).
    async def _on_startup(application: web.Application) -> None:
        _write_ready_marker(application[_KEY_WORKSPACE_ROOT])

    app.on_startup.append(_on_startup)
    app.router.add_get("/", _ws_handler)

    return app


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("RUNTIME_HOST", "0.0.0.0")
    port = int(os.environ.get("RUNTIME_PORT", "5959"))
    app = build_app()
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
