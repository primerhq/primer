"""Primer's model-only client for the peer-verified broker socket."""
from __future__ import annotations

import asyncio
import math
import os
import secrets
import socket
import sys
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import closing, contextmanager, suppress
from pathlib import Path

from primer.llm.codex.boundary import BoundaryError, BridgeConfig
from primer.llm.codex.lifecycle import OwnedStream, drain
from primer.llm.codex.request import ModelRequest, compact, decode
from primer.model.chat import Done, ReasoningDelta, StreamEvent, TextDelta, ToolCallEnd, ToolCallStart, Usage
from primer.model.providers._shared import Limits

from .host import HostTrust
from .delivery import digest, step_digest

_EVENTS = {
    "text_delta": TextDelta, "reasoning_delta": ReasoningDelta,
    "tool_call_start": ToolCallStart, "tool_call_end": ToolCallEnd,
    "usage": Usage, "done": Done,
}
_BOUNDS = BridgeConfig()


class BrokerClient:
    """One step/start per socket. Broker.step owns signed policy and ModelOnlyBridge."""

    def __init__(self, directory: Path, manifest_sha256: str, limits: Limits):
        self._directory = directory
        self._identity = manifest_sha256
        self._limits = limits
        self._active: set[OwnedStream] = set()
        self._discoveries: set[asyncio.Task] = set()
        self._closed = False
        # Fail during executor construction, before receiving a prompt. Keep
        # only the operator's pins in the registry, never an idle descriptor.
        with self._trust():
            pass

    @contextmanager
    def _trust(self) -> Iterator[HostTrust]:
        try:
            with closing(HostTrust(self._directory)) as trust:
                if trust.identity != self._identity or trust.primer_uid != os.geteuid():
                    raise BoundaryError("Codex broker host binding refused")
                yield trust
        except OSError:
            raise BoundaryError("Codex broker host trust unavailable") from None

    def _connect(self) -> socket.socket:
        # The host checks SO_PEERCRED before transferring the socket. Build
        # and dispose the thread-affine trust handle on this same thread.
        sock = None
        try:
            with self._trust() as trust:
                sock = trust.connect_model_socket()
            return sock
        except BaseException:
            if sock is not None:
                sock.close()
            raise

    async def _connected_socket(self) -> socket.socket:
        connecting = asyncio.create_task(asyncio.to_thread(self._connect))
        try:
            return await asyncio.shield(connecting)
        except asyncio.CancelledError as cancellation:
            # A cancelled await cannot cancel the bounded blocking connect.
            # Take back and dispose any socket produced after cancellation.
            with suppress(Exception, asyncio.CancelledError):
                await drain(connecting)
            if not connecting.cancelled() and connecting.exception() is None:
                try:
                    connecting.result().close()
                except OSError:
                    cancellation.add_note("Closing the cancelled broker connection failed")
            raise

    def stream(self, request: ModelRequest) -> OwnedStream:
        if self._closed:
            raise BoundaryError("Codex broker client is closed")
        deadline = _BOUNDS.deadline_seconds
        if self._limits.total_timeout_seconds is not None:
            deadline = min(deadline, self._limits.total_timeout_seconds)
        stream = OwnedStream(self._events(request), timeout=deadline, on_close=self._active.discard)
        self._active.add(stream)
        return stream

    async def models(self) -> list[dict[str, str]]:
        """Account/policy-filtered broker models, with no inferred capabilities.

        The host manifest chooses the account; neither the caller nor a
        catalogue file can select it. Require one complete bounded frame
        followed by EOF before exposing any results.
        """
        task = asyncio.current_task()
        if self._closed or task in self._discoveries:
            raise BoundaryError("Codex broker client is closed or reentered")
        self._discoveries.add(task)
        writer = None
        try:
            deadline = min(30.0, self._limits.total_timeout_seconds or 30.0)
            async with asyncio.timeout(deadline):
                async with asyncio.timeout(self._limits.connect_timeout_seconds):
                    sock = await self._connected_socket()
                    try:
                        reader, writer = await asyncio.open_unix_connection(sock=sock, limit=_BOUNDS.max_frame_bytes)
                    except BaseException:
                        sock.close()
                        raise
                    writer.write(b'{"method":"models/list","params":{}}\n')
                    await writer.drain()
                    line = await reader.readline()
                if not line.endswith(b"\n") or len(line) > _BOUNDS.max_frame_bytes:
                    raise BoundaryError("Invalid Codex model catalogue frame")
                frame = decode(line)
                if type(frame) is not dict or set(frame) != {"models"} or type(frame["models"]) is not list:
                    raise BoundaryError("Codex model discovery refused")
                if len(frame["models"]) > 4096:
                    raise BoundaryError("Codex model catalogue exceeds limit")
                models, seen = [], set()
                for item in frame["models"]:
                    if (type(item) is not dict or set(item) != {"slug", "display_name"}
                            or any(type(item[key]) is not str or not item[key] or len(item[key]) > 256
                                   for key in ("slug", "display_name")) or item["slug"] in seen):
                        raise BoundaryError("Invalid Codex model catalogue entry")
                    seen.add(item["slug"])
                    models.append({"name": item["slug"], "display_name": item["display_name"]})
                async with asyncio.timeout(self._limits.request_timeout_seconds):
                    if await reader.read(1):
                        raise BoundaryError("Unexpected trailing Codex catalogue data")
                if self._closed:
                    raise BoundaryError("Codex broker client closed during discovery")
                return models
        finally:
            failure = sys.exception()
            try:
                if writer is not None:
                    async def close():
                        writer.close()
                        try:
                            async with asyncio.timeout(_BOUNDS.cleanup_seconds):
                                await writer.wait_closed()
                        except BaseException:
                            writer.transport.abort()
                            raise
                    try:
                        await drain(asyncio.create_task(close()))
                    except BaseException:
                        if not isinstance(failure, asyncio.CancelledError):
                            raise
                        failure.add_note("Codex discovery cleanup failed during cancellation")
            finally:
                self._discoveries.discard(task)

    async def _events(self, request: ModelRequest) -> AsyncGenerator[StreamEvent, None]:
        nonce = secrets.token_hex(32)
        request_id = step_digest(request, nonce)
        payload = compact({"method": "step/start", "params": {
            "request": request.wire(), "aliases": request.aliases, "delivery_nonce": nonce,
        }}).encode() + b"\n"
        if len(payload) > _BOUNDS.max_frame_bytes:
            raise BoundaryError("Codex broker request exceeds frame limit")
        terminal: list[tuple[StreamEvent, str, str]] = []
        total = 0
        done = False
        writer = None
        try:
            # The connect budget includes the broker's first response. Later
            # reads each get the stall budget; neither timer spans a yield.
            async with asyncio.timeout(self._limits.connect_timeout_seconds):
                sock = await self._connected_socket()
                try:
                    reader, writer = await asyncio.open_unix_connection(sock=sock, limit=_BOUNDS.max_frame_bytes)
                except BaseException:
                    sock.close()
                    raise
                writer.write(payload)
                await writer.drain()
                line = await reader.readline()
            while line:
                total += len(line)
                if (done or not line.endswith(b"\n") or len(line) > _BOUNDS.max_frame_bytes
                        or total > _BOUNDS.max_output_bytes):
                    raise BoundaryError("Invalid Codex broker stream")
                frame = decode(line)
                if not isinstance(frame, dict) or not isinstance(frame.get("event"), dict):
                    raise BoundaryError("Codex broker step refused")
                data = frame["event"]
                cls = _EVENTS.get(data.get("type"))
                if cls is None or data.keys() - cls.model_fields.keys():
                    raise BoundaryError("Unknown Codex broker event")
                event = cls.model_validate(data, strict=True)
                if isinstance(event, (TextDelta, ReasoningDelta)):
                    if terminal or set(frame) != {"event"}:
                        raise BoundaryError("Unexpected delta in terminal Codex events")
                    yield event
                else:
                    # Receipts are not authorization. Complete the model stream
                    # first, then ask the broker afresh on each consumer advance.
                    if (set(frame) != {"event", "delivery"} or type(frame["delivery"]) is not str
                            or not 0 < len(frame["delivery"]) <= 8192):
                        raise BoundaryError("Missing Codex delivery receipt")
                    if len(terminal) >= 2 * _BOUNDS.max_calls + 2:
                        raise BoundaryError("Codex terminal batch exceeds limit")
                    terminal.append((event, frame["delivery"], digest(data)))
                    done = isinstance(event, Done)
                async with asyncio.timeout(self._limits.request_timeout_seconds):
                    line = await reader.readline()
            if not done:
                raise BoundaryError("Codex broker stream ended before completion")
        finally:
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(_BOUNDS.cleanup_seconds):
                        await writer.wait_closed()
                except BaseException:
                    writer.transport.abort()
                    raise
        for event, receipt, event_id in terminal:
            await self._authorize_delivery(receipt, request_id, event_id)
            if self._closed:
                raise BoundaryError("Codex broker client closed before delivery")
            yield event

    async def _authorize_delivery(self, receipt: str, request: str, event: str) -> None:
        """One fresh, peer-verified admission per caller-visible terminal event.

        The broker checks the original snapshot during this advancement; no
        previously buffered authorization reply is reused after a yield.
        """
        writer = None
        try:
            async with asyncio.timeout(self._limits.connect_timeout_seconds):
                sock = await self._connected_socket()
                try:
                    reader, writer = await asyncio.open_unix_connection(sock=sock, limit=1024)
                except BaseException:
                    sock.close()
                    raise
                writer.write(compact({"method": "step/authorize", "params": {
                    "receipt": receipt, "request": request, "event": event,
                }}).encode() + b"\n")
                await writer.drain()
                line = await reader.readline()
            async with asyncio.timeout(self._limits.request_timeout_seconds):
                if await reader.read(1):
                    raise BoundaryError("Unexpected Codex delivery authorization data")
            reply = decode(line)
            if (not line.endswith(b"\n") or len(line) > 1024 or type(reply) is not dict
                    or set(reply) != {"authorized", "expires_at", "deadline"}
                    or reply["authorized"] is not True
                    or any(type(reply[key]) not in {int, float} or not math.isfinite(reply[key])
                           for key in ("expires_at", "deadline"))):
                raise BoundaryError("Codex terminal delivery refused")
        finally:
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(_BOUNDS.cleanup_seconds):
                        await writer.wait_closed()
                except BaseException:
                    writer.transport.abort()
                    raise
        # Same-host Unix IPC shares the monotonic clock. Network/cleanup delay
        # cannot stretch the original step deadline or signed grant expiry.
        if self._closed or time.time() >= reply["expires_at"] or time.monotonic() >= reply["deadline"]:
            raise BoundaryError("Codex delivery authorization expired before delivery")

    async def aclose(self) -> None:
        if asyncio.current_task() in self._discoveries:
            raise BoundaryError("Codex discovery cannot close its own client")
        self._closed = True
        discoveries = tuple(self._discoveries)
        for task in discoveries:
            task.cancel()
        if discoveries:
            await asyncio.gather(*discoveries, return_exceptions=True)
        streams = tuple(self._active)
        for stream in streams:
            stream.ensure_can_close()
        results = await asyncio.gather(*(s.aclose() for s in streams), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
