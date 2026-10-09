"""Loopback servers for the hermetic tests of the keyed web adapters (ticket 01a12010): nothing leaves 127.0.0.1, no real host is involved.

``closing_server`` accepts a connection and closes it (httpcore connects before h11 checks a header, and an adapter that refuses to send a key makes no
connection at all: ``connections`` counts them). ``status_server`` answers every request with one canned status and body.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass
class Loopback:
    url: str = ""
    connections: int = 0


@asynccontextmanager
async def closing_server() -> AsyncIterator[Loopback]:
    """``http://127.0.0.1:<port>`` of a server that accepts a connection and closes it."""
    box = Loopback()

    async def accept_and_close(_reader, writer) -> None:
        box.connections += 1
        writer.close()

    server = await asyncio.start_server(accept_and_close, "127.0.0.1", 0)
    box.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield box
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def status_server(status: int, body: str) -> AsyncIterator[Loopback]:
    """A server that answers every request with ``status`` and ``body`` (it reads the request first, so the client never sees a reset)."""
    box = Loopback()
    payload = body.encode("utf-8")

    async def answer(reader, writer) -> None:
        box.connections += 1
        head = await reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        if length:
            await reader.readexactly(length)
        writer.write(
            b"HTTP/1.1 %d Error\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (status, len(payload)) + payload
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    box.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield box
    finally:
        server.close()
        await server.wait_closed()
