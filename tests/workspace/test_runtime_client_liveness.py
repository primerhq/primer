"""A runtime connection that stops answering is noticed, and nothing waits on it without a bound (review W1, W22, W55j, W55k).

* W1: ``ping`` and ``inspect`` sent a HEALTH request with no timeout. On a client whose reconnects keep failing (a stopped
  container) the request waited for a connection that never came, so ``ping`` never returned.
* W22: the heartbeat wrote a WebSocket PING frame and never checked that anything came back, so a half-open connection (a
  runtime that went silent with the TCP connection still up) kept reading as connected. It now sends a HEALTH request with a
  timeout, and three unanswered in a row force a reconnect.
* W55j: an idle stream woke ten times a second (``wait_for(q.get(), 0.1)``) to check its abort flag.
* W55k: ``open_pty`` left its stream slot behind when the request could not be sent.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from primer.workspace.runtime.protocol import OpName
from primer.workspace.runtime.runtime_client import _STREAM_CLOSED, RuntimeClient
from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeOpError
from primer.workspace.runtime.ws_sandbox import WSSandbox


class _FakeWs:
    """Records the frames the client sends; never answers."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_str(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self) -> None:
        self.closed = True


def _connected_client() -> tuple[RuntimeClient, _FakeWs]:
    client = RuntimeClient(url="ws://x/", token="t")
    ws = _FakeWs()
    client._ws = ws  # type: ignore[assignment]
    client._connected.set()
    return client, ws


class _CountingQueue(asyncio.Queue):
    """Counts how often the stream asks for its next item."""

    def __init__(self) -> None:
        super().__init__()
        self.gets = 0

    async def get(self):  # type: ignore[override]
        self.gets += 1
        return await super().get()


# ---- characterization: what stays as it was ---------------------------------------------------------------------------------------


async def test_a_ping_that_is_answered_returns() -> None:
    client, ws = _connected_client()
    task = asyncio.create_task(client.ping())
    for _ in range(100):
        if ws.sent:
            break
        await asyncio.sleep(0.01)
    (frame,) = ws.sent
    assert frame["op"] == OpName.HEALTH and frame["args"] == {}
    client._dispatch_text(json.dumps({"req_id": frame["req_id"], "ok": True, "result": {"ok": True}}))
    assert await asyncio.wait_for(task, timeout=2) is None


async def test_a_stream_yields_its_items_in_order_and_ends_when_closed() -> None:
    client, _ = _connected_client()
    q: asyncio.Queue = asyncio.Queue()
    for item in ({"event": "a"}, b"bytes", {"event": "b"}, _STREAM_CLOSED, {"event": "after"}):
        q.put_nowait(item)
    got = [item async for item in client._iter_stream(1, q)]
    assert got == [{"event": "a"}, b"bytes", {"event": "b"}]


async def test_an_abort_ends_an_idle_stream() -> None:
    client, _ = _connected_client()
    q: asyncio.Queue = asyncio.Queue()
    abort = asyncio.Event()
    task = asyncio.create_task(_drain(client, q, abort))
    await asyncio.sleep(0.05)
    abort.set()
    assert await asyncio.wait_for(task, timeout=2) == []
    assert q.empty()


async def test_an_abort_set_before_the_stream_starts_yields_nothing() -> None:
    client, _ = _connected_client()
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"event": "a"})
    abort = asyncio.Event()
    abort.set()
    assert await _drain(client, q, abort) == []


async def test_an_item_that_arrives_while_the_stream_waits_with_an_abort_is_yielded() -> None:
    client, _ = _connected_client()
    q: asyncio.Queue = asyncio.Queue()
    abort = asyncio.Event()
    task = asyncio.create_task(_drain(client, q, abort))
    await asyncio.sleep(0.05)
    q.put_nowait({"event": "a"})
    q.put_nowait(_STREAM_CLOSED)
    assert await asyncio.wait_for(task, timeout=2) == [{"event": "a"}]


async def test_open_pty_sends_the_open_and_keeps_its_stream() -> None:
    client, ws = _connected_client()
    handle = await client.open_pty(cmd=["/bin/sh"], cols=100, rows=30, workdir="/w", env={"A": "1"})
    (frame,) = ws.sent
    assert frame["op"] == OpName.PTY_OPEN
    assert frame["args"] == {"cols": 100, "rows": 30, "cmd": ["/bin/sh"], "workdir": "/w", "env": {"A": "1"}}
    assert list(client._streams) == [frame["req_id"]]
    task = asyncio.create_task(handle.close())
    for _ in range(100):
        if len(ws.sent) == 2:
            break
        await asyncio.sleep(0.01)
    close = ws.sent[1]
    client._dispatch_text(json.dumps({"req_id": close["req_id"], "ok": True, "result": {}}))
    await asyncio.wait_for(task, timeout=2)
    assert client._streams == {}


# ---- W1: a request on a client that cannot connect gives up ----------------------------------------------------------------------


async def test_a_ping_on_a_client_that_never_connects_answers_false(monkeypatch) -> None:
    monkeypatch.setattr(RuntimeClient, "_HEALTH_TIMEOUT_S", 0.2, raising=False)
    client = RuntimeClient(url="ws://127.0.0.1:1/", token="t")      # never connected: a client whose reconnects keep failing
    sandbox = WSSandbox(runtime_client=client, container_id="c")
    try:
        assert await asyncio.wait_for(sandbox.ping(), timeout=3) is False
        assert (await asyncio.wait_for(sandbox.inspect(), timeout=3)).state == "unknown"
        assert client._pending == {}
    finally:
        await client.aclose()


# ---- W22: the heartbeat notices a runtime that stopped answering -----------------------------------------------------------------


async def test_a_silent_runtime_is_reconnected_after_three_unanswered_heartbeats(monkeypatch) -> None:
    connections = 0
    stop = asyncio.Event()

    async def handler(request):
        nonlocal connections
        connections += 1
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        hello = json.loads((await ws.receive()).data)
        await ws.send_str(json.dumps({"req_id": hello["req_id"], "ok": True, "result": {"protocol": "1.4"}}))
        await stop.wait()  # silent: never reads, never answers, keeps the TCP connection open
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(RuntimeClient, "_HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(RuntimeClient, "_HEALTH_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setattr(RuntimeClient, "_RECONNECT_DELAYS", (0.05,))
    client = RuntimeClient(url=f"ws://127.0.0.1:{port}/", token="t")
    await client.connect()
    try:
        for _ in range(200):
            if connections >= 2:
                break
            await asyncio.sleep(0.025)
        assert connections >= 2, "the heartbeat never noticed the silent runtime"
    finally:
        await client.aclose()
        stop.set()
        await runner.cleanup()


# ---- W55j: an idle stream is not polled -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("with_abort", [True, False], ids=["abort", "no-abort"])
async def test_an_idle_stream_waits_without_waking(with_abort: bool) -> None:
    client, _ = _connected_client()
    q = _CountingQueue()
    abort = asyncio.Event() if with_abort else None
    task = asyncio.create_task(_drain(client, q, abort))
    await asyncio.sleep(0.5)
    assert q.gets <= 1, f"the idle stream asked its queue {q.gets} times in half a second"
    q.put_nowait({"event": "a"})
    q.put_nowait(_STREAM_CLOSED)
    assert await asyncio.wait_for(task, timeout=2) == [{"event": "a"}]


async def _drain(client: RuntimeClient, q: asyncio.Queue, abort: asyncio.Event | None) -> list:
    return [item async for item in client._iter_stream(1, q, abort=abort)]
