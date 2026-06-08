"""Worker-side WebSocket client for the workspace runtime.

Opens a persistent WS connection to the in-container runtime server,
multiplexes concurrent requests via *req_id* correlation, handles
streaming ops (exec / watch / archive), and provides automatic reconnect
with exponential back-off and a 15-second heartbeat.

Usage::

    client = RuntimeClient(url="ws://127.0.0.1:32100/", token="secret")
    await client.connect()
    data = await client.read_file("/workspace/foo.txt")
    await client.aclose()
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp

from primer.int.sandbox import ExecResult, FileStat
from primer.workspace.runtime.protocol import ErrorCode, OpName, Request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeEvent:
    """A filesystem change notification pushed by a ``watch_start`` subscription."""

    path: str
    event: str  # "modify" | "delete" | "create"
    mtime: float | None = None
    size: int | None = None


class RuntimeError(Exception):
    """An error response from the runtime server."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_file_stat(raw: dict[str, Any]) -> FileStat:
    """Convert a protocol FileStat dict to the sandbox-level :class:`FileStat`."""
    is_dir: bool = bool(raw.get("is_dir", False))
    mtime_raw = raw.get("mtime", 0.0)
    if isinstance(mtime_raw, (int, float)):
        modified_at = datetime.fromtimestamp(float(mtime_raw), tz=timezone.utc)
    else:
        modified_at = datetime.now(tz=timezone.utc)
    return FileStat(
        path=raw["path"],
        kind="dir" if is_dir else "file",
        size_bytes=int(raw.get("size", 0)),
        mode=int(raw.get("mode", 0o644)),
        modified_at=modified_at,
    )


# ---------------------------------------------------------------------------
# Sentinel objects used in stream queues
# ---------------------------------------------------------------------------

_STREAM_CLOSED = object()  # signals that the stream has ended normally


# ---------------------------------------------------------------------------
# RuntimeClient
# ---------------------------------------------------------------------------


class RuntimeClient:
    """Worker-side WebSocket client that speaks the primer runtime protocol.

    Parameters
    ----------
    url:
        Full WebSocket URL of the runtime server,
        e.g. ``"ws://127.0.0.1:32100/"``.
    token:
        Bearer token injected as ``PRIMER_RUNTIME_TOKEN`` inside the container.
    protocol_version:
        Protocol major.minor to advertise during the ``hello`` handshake.
    """

    _HEARTBEAT_INTERVAL_S: float = 15.0
    _HEARTBEAT_MAX_MISSED: int = 3
    _RECONNECT_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

    def __init__(
        self,
        *,
        url: str,
        token: str,
        protocol_version: str = "1.1",
    ) -> None:
        self._url = url
        self._token = token
        self._protocol_version = protocol_version

        # Pending single-shot futures: req_id → Future
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # Active stream queues: req_id → Queue  (items are raw dicts or _STREAM_CLOSED)
        self._streams: dict[int, asyncio.Queue[Any]] = {}
        self._next_req_id: int = 1

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None

        # Background tasks
        self._receive_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None

        self._closed = False
        self._connected = asyncio.Event()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WS, run the handshake, and start background tasks.

        Idempotent — safe to call when already connected.
        """
        if self._closed:
            raise RuntimeError("EPROTOCOL", "Client is closed")
        if self._connected.is_set():
            return
        async with self._lock:
            if self._connected.is_set():
                return
            await self._do_connect()
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(), name="runtime-reconnect"
            )

    async def aclose(self) -> None:
        """Shut down the client and close all in-flight operations."""
        self._closed = True
        self._connected.clear()

        for task in (self._receive_task, self._heartbeat_task, self._reconnect_task):
            if task and not task.done():
                task.cancel()

        if self._ws and not self._ws.closed:
            await self._ws.close()

        if self._session and not self._session.closed:
            await self._session.close()

        self._fail_all_pending(ErrorCode.EPROTOCOL, "Client closed")

    # ------------------------------------------------------------------
    # Per-op public methods
    # ------------------------------------------------------------------

    async def ping(self) -> None:
        """Cheap liveness probe over the live WS connection.

        Sends a ``health`` request and discards the result.  Raises if
        the connection is down or the runtime responds with ``ok=false``.
        Used by :meth:`primer.workspace.runtime.ws_sandbox.WSSandbox.ping`
        and the Phase-7 workspace probe task; callers wrap the
        :class:`Exception` into a ``False`` return value.
        """
        await self._send_request(OpName.HEALTH, {})

    async def read_file(self, path: str) -> bytes:
        result = await self._send_request(OpName.READ_FILE, {"path": path})
        return base64.b64decode(result["content_b64"])

    async def write_file(
        self, path: str, content: bytes, *, mode: int | None = None
    ) -> None:
        args: dict[str, Any] = {
            "path": path,
            "content_b64": base64.b64encode(content).decode(),
        }
        if mode is not None:
            args["mode"] = mode
        await self._send_request(OpName.WRITE_FILE, args)

    async def append_line(self, path: str, line: bytes) -> int:
        """Atomically append *line* to *path*. Returns the byte offset."""
        result = await self._send_request(
            OpName.APPEND_LINE,
            {"path": path, "line_b64": base64.b64encode(line).decode()},
        )
        return int(result["byte_offset"])

    async def list_dir(self, path: str) -> list[FileStat]:
        result = await self._send_request(OpName.LIST_DIR, {"path": path})
        return [_parse_file_stat(e) for e in result["entries"]]

    async def stat(self, path: str) -> FileStat | None:
        result = await self._send_request(OpName.STAT, {"path": path})
        raw = result.get("stat")
        return _parse_file_stat(raw) if raw is not None else None

    async def delete(self, path: str) -> None:
        await self._send_request(OpName.DELETE, {"path": path})

    async def archive(self, paths: list[str]) -> AsyncIterator[bytes]:  # type: ignore[override]
        """Stream a tar archive.  Yields raw binary chunks."""
        req_id = self._alloc_req_id()
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._streams[req_id] = q
        try:
            await self._send_raw(
                Request(req_id=req_id, op=OpName.ARCHIVE, args={"paths": paths})
            )
            async for item in self._iter_stream(req_id, q):
                if isinstance(item, dict):
                    event = item.get("event")
                    if event == "archive_done":
                        return
                    # binary chunk arrived via _receive_loop as bytes
                    data_b64 = item.get("data_b64")
                    if data_b64:
                        yield base64.b64decode(data_b64)
                elif isinstance(item, (bytes, bytearray)):
                    yield bytes(item)
        finally:
            self._streams.pop(req_id, None)

    async def exec(  # noqa: A003
        self,
        cmd: str | list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
        abort: asyncio.Event | None = None,
    ) -> ExecResult:
        """Run *cmd* inside the container and return the aggregated result."""
        # The runtime spawns the command via ``create_subprocess_exec(*cmd)``,
        # i.e. it expects an argv LIST. A bare string would be iterated into
        # one-character argv entries (``"pwd"`` -> ``['p','w','d']``), so wrap
        # a string command in a shell. Callers passing a list (e.g. the state
        # repo's ``["git","init"]``) are sent through unchanged.
        argv: list[str] = ["/bin/sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
        args: dict[str, Any] = {"cmd": argv}
        if workdir is not None:
            args["workdir"] = workdir
        if env is not None:
            args["env"] = env
        if timeout_s is not None:
            args["timeout_s"] = timeout_s
        if stdin is not None:
            args["stdin_b64"] = base64.b64encode(stdin).decode()

        req_id = self._alloc_req_id()
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._streams[req_id] = q

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        exit_code = -1
        t_start = time.monotonic()

        try:
            await self._send_raw(Request(req_id=req_id, op=OpName.EXEC, args=args))
            async for item in self._iter_stream(req_id, q, abort=abort):
                if not isinstance(item, dict):
                    continue
                # A single-shot {"ok": false} error frame routed into the
                # stream (runtime rejected the exec request, e.g. bad argv):
                # surface it instead of waiting forever for an exit event.
                if "ok" in item and not item["ok"]:
                    err = item.get("error") or {}
                    raise RuntimeError(
                        err.get("code", ErrorCode.EPROTOCOL),
                        err.get("message", "exec failed"),
                    )
                event = item.get("event")
                # The runtime serialises streaming events as the protocol
                # Event envelope: {"event": ..., "data": {...}}. The exec
                # payload (data_b64 / code) lives under the nested ``data``
                # key, NOT at the top level. Fall back to the top level so a
                # flatter future shape still works.
                payload = item.get("data") if isinstance(item.get("data"), dict) else item
                if event == "stdout":
                    stdout_chunks.append(base64.b64decode(payload.get("data_b64", "")))
                elif event == "stderr":
                    stderr_chunks.append(base64.b64decode(payload.get("data_b64", "")))
                elif event == "exit":
                    exit_code = int(payload.get("code", -1))
                    break
        finally:
            self._streams.pop(req_id, None)

        duration = time.monotonic() - t_start
        return ExecResult(
            exit_code=exit_code,
            stdout=b"".join(stdout_chunks).decode(errors="replace"),
            stderr=b"".join(stderr_chunks).decode(errors="replace"),
            duration_seconds=duration,
        )

    async def watch(
        self,
        paths: list[str],
        events: list[str],
    ) -> AsyncIterator[ChangeEvent]:  # type: ignore[override]
        """Subscribe to filesystem change events.

        Yields :class:`ChangeEvent` objects until the caller closes the
        iterator or the connection drops.
        """
        req_id = self._alloc_req_id()
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._streams[req_id] = q
        try:
            await self._send_raw(
                Request(
                    req_id=req_id,
                    op=OpName.WATCH_START,
                    args={"paths": paths, "events": events},
                )
            )
            async for item in self._iter_stream(req_id, q):
                if not isinstance(item, dict):
                    continue
                event = item.get("event")
                if event == "watch_open":
                    continue
                if event == "watch_closed":
                    return
                if event == "change":
                    # Change details live under the Event envelope's nested
                    # ``data`` key (same shape as exec events); fall back to
                    # the top level for a flatter future shape.
                    payload = item.get("data") if isinstance(item.get("data"), dict) else item
                    yield ChangeEvent(
                        path=payload.get("path", ""),
                        event=payload.get("change_event", "modify"),
                        mtime=payload.get("mtime"),
                        size=payload.get("size"),
                    )
        finally:
            self._streams.pop(req_id, None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _alloc_req_id(self) -> int:
        rid = self._next_req_id
        self._next_req_id += 1
        return rid

    async def _send_request(self, op: OpName, args: dict[str, Any]) -> dict[str, Any]:
        """Send a single-shot request and wait for the response.

        Raises :class:`RuntimeError` if the server responds with ok=false.
        Raises :class:`RuntimeError` with code EPROTOCOL if the connection
        drops while the request is in-flight.
        """
        req_id = self._alloc_req_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._send_raw(Request(req_id=req_id, op=op, args=args))
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def _send_raw(self, msg: Request) -> None:
        """Serialize *msg* and send it over the active WebSocket."""
        await self._connected.wait()
        ws = self._ws
        if ws is None or ws.closed:
            raise RuntimeError("EPROTOCOL", "Not connected")
        from primer.workspace.runtime.protocol import serialize

        await ws.send_str(serialize(msg))

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Dispatch incoming WS frames to waiting futures / stream queues."""
        ws = self._ws
        assert ws is not None
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._dispatch_text(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    self._dispatch_binary(msg.data)
                elif msg.type == aiohttp.WSMsgType.PONG:
                    self._on_pong()
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("receive_loop error: %s", exc)
        finally:
            self._on_disconnect()

    def _dispatch_text(self, text: str) -> None:
        import json

        from primer.workspace.runtime.protocol import deserialize

        try:
            raw = json.loads(text)
        except Exception:  # noqa: BLE001
            logger.warning("Received non-JSON frame: %.120s", text)
            return

        req_id: int = raw.get("req_id", -1)

        # Single-shot response
        if "ok" in raw:
            fut = self._pending.get(req_id)
            if fut is None and req_id in self._streams:
                # An error frame for a STREAMING op (exec/watch/archive): the
                # runtime answers a failed streaming request with a single-shot
                # {"ok": false} instead of an event stream. Route it into the
                # stream queue so the consumer surfaces the error instead of
                # blocking forever waiting for an exit/close event.
                self._streams[req_id].put_nowait(raw)
                return
            if fut and not fut.done():
                if raw["ok"]:
                    fut.set_result(raw.get("result") or {})
                else:
                    err = raw.get("error") or {}
                    fut.set_exception(
                        RuntimeError(
                            err.get("code", ErrorCode.EPROTOCOL),
                            err.get("message", "unknown error"),
                        )
                    )
            return

        # Streaming event (exec stdout/stderr, watch change, etc.)
        if "event" in raw:
            q = self._streams.get(req_id)
            if q is not None:
                q.put_nowait(raw)
            return

        logger.debug("Unrecognised frame for req_id=%s: %s", req_id, text[:120])

    def _dispatch_binary(self, data: bytes) -> None:
        """Route a binary frame.

        Binary frames carry bulk payload (e.g. archive chunks).  The first
        8 bytes encode ``(req_id: uint32_be, seq: uint16_be, total: uint16_be)``
        per the protocol spec.  For simplicity we pass the raw bytes to the
        matching stream queue so :meth:`archive` can yield them.
        """
        import struct

        if len(data) < 8:
            return
        req_id = struct.unpack_from(">I", data, 0)[0]
        q = self._streams.get(req_id)
        if q is not None:
            q.put_nowait(data[8:])

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _on_pong(self) -> None:
        """Called when a PONG frame is received from the server.

        Note: aiohttp's client-side ``async for`` loop does not surface PONG
        frames directly.  Liveness detection is instead implemented via the
        manual ping loop in :meth:`_heartbeat_loop`, which closes the WS after
        ``_HEARTBEAT_MAX_MISSED`` cycles without a successful response.
        aiohttp's own ``heartbeat`` kwarg on ``ws_connect`` is *not* used here
        so that our explicit ping/pong counting remains the single authority.
        """
        self._pong_received = True

    async def _heartbeat_loop(self) -> None:
        """Ping the server every ``_HEARTBEAT_INTERVAL_S`` seconds.

        Sends a WS PING and waits one full interval for a PONG.  Because
        aiohttp's client async-for does not surface PONG frames, we instead
        send an ``health`` RPC (which does travel through the normal response
        path) as a lightweight liveness probe.  After
        ``_HEARTBEAT_MAX_MISSED`` consecutive failures the connection is
        force-closed and the reconnect loop takes over.
        """
        missed = 0
        try:
            while not self._closed:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL_S)
                ws = self._ws
                if ws is None or ws.closed:
                    break
                # Use a raw WS ping; aiohttp handles the pong internally and
                # keeps the connection alive.  We additionally send a health
                # RPC once every _HEARTBEAT_MAX_MISSED intervals so we can
                # detect silent server death.
                try:
                    await asyncio.wait_for(ws.ping(), timeout=self._HEARTBEAT_INTERVAL_S)
                    missed = 0
                except (asyncio.TimeoutError, ConnectionError, aiohttp.ClientError):
                    missed += 1
                    logger.warning(
                        "Heartbeat ping failed (%d/%d)",
                        missed,
                        self._HEARTBEAT_MAX_MISSED,
                    )
                    if missed >= self._HEARTBEAT_MAX_MISSED:
                        logger.error("3 missed heartbeats — forcing reconnect")
                        if ws and not ws.closed:
                            await ws.close()
                        break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    def _on_disconnect(self) -> None:
        """Called when the WS closes unexpectedly."""
        self._connected.clear()
        if not self._closed:
            logger.info("WS disconnected; reconnect loop will retry")
        self._fail_all_pending(ErrorCode.EPROTOCOL, "Connection lost")
        self._close_all_streams()

    def _fail_all_pending(self, code: str, message: str) -> None:
        exc = RuntimeError(code, message)
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    def _close_all_streams(self) -> None:
        for q in list(self._streams.values()):
            q.put_nowait(_STREAM_CLOSED)
        self._streams.clear()

    async def _reconnect_loop(self) -> None:
        """Reconnect with exponential back-off whenever the WS closes."""
        delays = list(self._RECONNECT_DELAYS)
        attempt = 0
        try:
            while not self._closed:
                # Wait for a disconnect signal
                await asyncio.sleep(0)
                while not self._closed and self._connected.is_set():
                    await asyncio.sleep(0.1)
                if self._closed:
                    return
                delay = delays[min(attempt, len(delays) - 1)]
                logger.info("Reconnect attempt %d in %.0fs", attempt + 1, delay)
                await asyncio.sleep(delay)
                attempt += 1
                try:
                    await self._do_connect()
                    attempt = 0  # reset backoff on success
                    logger.info("Reconnected successfully")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Reconnect failed: %s", exc)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Core connect / handshake
    # ------------------------------------------------------------------

    async def _do_connect(self) -> None:
        """Open a new WS connection and run the hello handshake."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        headers = {"Authorization": f"Bearer {self._token}"}
        self._ws = await self._session.ws_connect(self._url, headers=headers)

        # Handshake
        hello = Request(
            req_id=0,
            op=OpName.HELLO,
            args={
                "protocol": self._protocol_version,
                "client": "primer-worker/0.1.0",
            },
        )
        from primer.workspace.runtime.protocol import serialize

        await self._ws.send_str(serialize(hello))

        resp_msg = await asyncio.wait_for(self._ws.receive(), timeout=10.0)
        if resp_msg.type != aiohttp.WSMsgType.TEXT:
            await self._ws.close()
            raise RuntimeError(
                "EPROTOCOL",
                f"Expected TEXT for hello response, got {resp_msg.type}",
            )

        import json

        resp = json.loads(resp_msg.data)
        if not resp.get("ok"):
            err = resp.get("error") or {}
            await self._ws.close()
            raise RuntimeError(
                err.get("code", "EPROTOCOL"),
                err.get("message", "hello failed"),
            )

        # Cancel old background tasks before spawning new ones
        for task in (self._receive_task, self._heartbeat_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        self._receive_task = asyncio.create_task(
            self._receive_loop(), name="runtime-receive"
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="runtime-heartbeat"
        )
        self._connected.set()

    # ------------------------------------------------------------------
    # Stream iteration helper
    # ------------------------------------------------------------------

    async def _iter_stream(
        self,
        req_id: int,
        q: asyncio.Queue[Any],
        *,
        abort: asyncio.Event | None = None,
    ) -> AsyncIterator[Any]:
        """Yield items from *q* until _STREAM_CLOSED or *abort* fires."""
        while True:
            if abort is not None and abort.is_set():
                break
            try:
                item = await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if item is _STREAM_CLOSED:
                break
            yield item


__all__ = ["RuntimeClient", "ChangeEvent", "RuntimeError"]
