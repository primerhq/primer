"""Tests for runtime/primer_runtime/server.py — handshake + auth."""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from primer_runtime.server import build_app, PROTOCOL_VERSION


class WSAuthError(Exception):
    """Raised when the server rejects our bearer token (HTTP 401)."""


class ServerFixture:
    """Wraps a running aiohttp TestServer and provides a .client() context manager."""

    def __init__(self, server: TestServer) -> None:
        self._server = server

    @asynccontextmanager
    async def client(self, *, token: str) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
        url = self._server.make_url("/")
        session = aiohttp.ClientSession()
        try:
            try:
                ws = await session.ws_connect(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status == 401:
                    raise WSAuthError(f"Auth rejected: {exc}") from exc
                raise
            try:
                yield ws
            finally:
                await ws.close()
        finally:
            await session.close()


@pytest_asyncio.fixture
async def server(tmp_path) -> AsyncIterator[ServerFixture]:
    """Start the aiohttp app with a known token; yield a ServerFixture."""
    token = "abc123"
    app = build_app(token=token, workspace_root=str(tmp_path))
    test_server = TestServer(app)
    await test_server.start_server()
    yield ServerFixture(test_server)
    await test_server.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handshake_correct_token_correct_protocol(server: ServerFixture) -> None:
    async with server.client(token="abc123") as ws:
        await ws.send_json(
            {
                "req_id": 0,
                "op": "hello",
                "args": {"protocol": "1.0", "client": "test/0"},
            }
        )
        resp = await ws.receive_json()
        assert resp["ok"] is True
        assert resp["result"]["protocol"] == "1.0"


@pytest.mark.asyncio
async def test_handshake_wrong_token_rejected(server: ServerFixture) -> None:
    with pytest.raises(WSAuthError):
        async with server.client(token="WRONG"):
            pass


@pytest.mark.asyncio
async def test_handshake_major_mismatch_closes_4400(server: ServerFixture) -> None:
    async with server.client(token="abc123") as ws:
        await ws.send_json(
            {
                "req_id": 0,
                "op": "hello",
                "args": {"protocol": "2.0", "client": "test/0"},
            }
        )
        msg = await ws.receive()
        # The server should close with code 4400
        assert msg.type == aiohttp.WSMsgType.CLOSE
        assert ws.close_code == 4400


@pytest.mark.asyncio
async def test_runtime_ready_marker_written(tmp_path, server: ServerFixture) -> None:
    """The .runtime.ready file should exist once the server is up."""
    ready_file = tmp_path / ".runtime.ready"
    assert ready_file.exists(), f"{ready_file} was not written"


@pytest.mark.asyncio
async def test_unknown_op_returns_eunsupported(server: ServerFixture) -> None:
    async with server.client(token="abc123") as ws:
        # First do the handshake
        await ws.send_json(
            {
                "req_id": 0,
                "op": "hello",
                "args": {"protocol": "1.0", "client": "test/0"},
            }
        )
        _ = await ws.receive_json()  # consume hello response

        # Send an op that is not yet implemented (archive, exec, etc.)
        await ws.send_json(
            {
                "req_id": 1,
                "op": "archive",
                "args": {"paths": []},
            }
        )
        resp = await ws.receive_json()
        assert resp["ok"] is False
        assert resp["error"]["code"] == "EUNSUPPORTED"
