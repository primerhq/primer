"""EVENTS_SUBSCRIBE: exec lifecycle broadcast + file changes.

Uses the same TestServer harness as test_server.py; two concurrent
connections prove the broadcast crosses connections (a subscriber on
one sees an exec requested on the other).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

from primer_runtime.server import PROTOCOL_VERSION, build_app


class ServerFixture:
    def __init__(self, server: TestServer) -> None:
        self._server = server

    @asynccontextmanager
    async def client(self, *, token: str) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
        url = self._server.make_url("/")
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(
                url, headers={"Authorization": f"Bearer {token}"},
            )
            try:
                yield ws
            finally:
                await ws.close()
        finally:
            await session.close()


@pytest_asyncio.fixture
async def server(tmp_path) -> AsyncIterator[ServerFixture]:
    app = build_app(token="abc123", workspace_root=str(tmp_path))
    test_server = TestServer(app)
    await test_server.start_server()
    yield ServerFixture(test_server)
    await test_server.close()


async def _hello(ws) -> None:
    await ws.send_json({
        "req_id": 0, "op": "hello",
        "args": {"protocol": PROTOCOL_VERSION, "client": "test/0"},
    })
    resp = await ws.receive_json()
    assert resp["ok"] is True


async def _next_ws_event(ws, *, timeout: float = 5.0) -> dict:
    """Drain frames until a ws_event arrives."""
    while True:
        frame = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        if frame.get("event") == "ws_event":
            return frame
        # exec stdout/exit/ok frames for our own requests: skip.


@pytest.mark.asyncio
async def test_exec_lifecycle_broadcasts_across_connections(
    server: ServerFixture,
) -> None:
    async with server.client(token="abc123") as sub_ws:
        await _hello(sub_ws)
        await sub_ws.send_json({
            "req_id": 7, "op": "events_subscribe",
            "args": {"kinds": ["exec_started", "exec_exited"]},
        })
        ack = await sub_ws.receive_json()
        assert ack["ok"] is True
        assert ack["result"]["kinds"] == ["exec_exited", "exec_started"]

        async with server.client(token="abc123") as exec_ws:
            await _hello(exec_ws)
            await exec_ws.send_json({
                "req_id": 1, "op": "exec",
                "args": {"cmd": ["/bin/sh", "-c", "true"],
                         "access": "read"},
            })

            started = await _next_ws_event(sub_ws)
            assert started["req_id"] == 7
            assert started["data"]["kind"] == "exec_started"
            assert started["data"]["cmd"][-1] == "true"

            exited = await _next_ws_event(sub_ws)
            assert exited["data"]["kind"] == "exec_exited"
            assert exited["data"]["exit_code"] == 0
            assert exited["data"]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_file_changed_kind_streams_changes(server: ServerFixture, tmp_path) -> None:
    async with server.client(token="abc123") as ws:
        await _hello(ws)
        await ws.send_json({
            "req_id": 9, "op": "events_subscribe",
            "args": {"kinds": ["file_changed"], "path_prefixes": ["."]},
        })
        # ack + watch_open arrive (order: ack response, then watch_open).
        seen_open = False
        for _ in range(2):
            frame = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
            if frame.get("event") == "watch_open":
                seen_open = True
        assert seen_open

        (tmp_path / "probe.txt").write_text("hello")
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
            if frame.get("event") == "change":
                assert frame["req_id"] == 9
                assert frame["data"]["path"].endswith("probe.txt")
                break


@pytest.mark.asyncio
async def test_subscriber_disconnect_is_cleaned_up(server: ServerFixture) -> None:
    """A gone subscriber never breaks later execs."""
    async with server.client(token="abc123") as sub_ws:
        await _hello(sub_ws)
        await sub_ws.send_json({
            "req_id": 3, "op": "events_subscribe",
            "args": {"kinds": ["exec_started"]},
        })
        assert (await sub_ws.receive_json())["ok"] is True
    # subscriber closed; run an exec on a fresh connection
    async with server.client(token="abc123") as exec_ws:
        await _hello(exec_ws)
        await exec_ws.send_json({
            "req_id": 1, "op": "exec",
            "args": {"cmd": ["/bin/sh", "-c", "true"], "access": "read"},
        })
        frames = []
        while True:
            frame = await asyncio.wait_for(exec_ws.receive_json(), timeout=5.0)
            frames.append(frame)
            if frame.get("event") == "exit":
                assert frame["data"]["code"] == 0
                break
