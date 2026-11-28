"""Primer's model-only client for the peer-verified broker socket."""
from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncGenerator, Iterator
from contextlib import closing, contextmanager, suppress
from pathlib import Path

from primer.llm.codex.boundary import BoundaryError, BridgeConfig
from primer.llm.codex.lifecycle import OwnedStream, drain
from primer.llm.codex.request import ModelRequest, compact, decode
from primer.model.chat import Done, ReasoningDelta, StreamEvent, TextDelta, ToolCallEnd, ToolCallStart, Usage
from primer.model.providers._shared import Limits

from .host import HostTrust

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

    async def _events(self, request: ModelRequest) -> AsyncGenerator[StreamEvent, None]:
        payload = compact({"method": "step/start", "params": {
            "request": request.wire(), "aliases": request.aliases,
        }}).encode() + b"\n"
        if len(payload) > _BOUNDS.max_frame_bytes:
            raise BoundaryError("Codex broker request exceeds frame limit")
        terminal: list[StreamEvent] = []
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
                if not isinstance(frame, dict) or set(frame) != {"event"} or not isinstance(frame["event"], dict):
                    raise BoundaryError("Codex broker step refused")
                data = frame["event"]
                cls = _EVENTS.get(data.get("type"))
                if cls is None or data.keys() - cls.model_fields.keys():
                    raise BoundaryError("Unknown Codex broker event")
                event = cls.model_validate(data, strict=True)
                if isinstance(event, (TextDelta, ReasoningDelta)):
                    if terminal:
                        raise BoundaryError("Unexpected delta in terminal Codex events")
                    yield event
                else:
                    # Propagate tool calls only after the broker confirms the
                    # entire terminal batch, including its signed-policy checks.
                    if len(terminal) >= 2 * _BOUNDS.max_calls + 2:
                        raise BoundaryError("Codex terminal batch exceeds limit")
                    terminal.append(event)
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
        for event in terminal:
            yield event

    async def aclose(self) -> None:
        self._closed = True
        streams = tuple(self._active)
        for stream in streams:
            stream.ensure_can_close()
        results = await asyncio.gather(*(s.aclose() for s in streams), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
