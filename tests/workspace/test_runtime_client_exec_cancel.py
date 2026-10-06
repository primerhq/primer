"""``RuntimeClient.exec`` tells the runtime to stop the command when it is left early (protocol 1.4 ``exec_cancel``).

Before, a consumer that was cancelled (a Stop, a Cancel, a server shutdown) or an ``abort`` that fired only dropped the
client's stream: the command kept running in the container until its own timeout or until the connection closed, holding
the workspace write lock. Now the client sends ``exec_cancel`` with the exec's req_id, under the rules the lead set:

* gated on the version the SERVER reported in the handshake (``negotiated_version``), never on the version the client
  advertised: an older runtime answers an unknown op with EUNSUPPORTED, so it is simply not sent;
* fire-and-forget, from a SEPARATE task: the task being cancelled must not await the send (a second cancel would
  interrupt it, and a stalled socket would hold the cancel up);
* only for an exec that was left before its exit event: a finished exec, and one the runtime rejected, are not cancelled;
* only on the connection the exec was sent on: after a reconnect the old runtime-side exec is gone with its connection.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from primer.workspace.runtime.protocol import OpName
from primer.workspace.runtime.runtime_client import RuntimeClient
from primer.workspace.runtime.runtime_client import RuntimeError as RuntimeOpError


class _FakeWs:
    """Records the frames the client sends. ``stall_cancels`` makes sending an ``exec_cancel`` frame hang for ever."""

    def __init__(self, *, stall_cancels: bool = False) -> None:
        self.closed = False
        self.sent: list[dict] = []
        self.stall_cancels = stall_cancels

    async def send_str(self, text: str) -> None:
        frame = json.loads(text)
        if self.stall_cancels and frame["op"] == OpName.EXEC_CANCEL:
            await asyncio.Event().wait()
        self.sent.append(frame)

    async def close(self) -> None:
        self.closed = True


def _client(version: str = "1.4", ws: _FakeWs | None = None) -> tuple[RuntimeClient, _FakeWs]:
    client = RuntimeClient(url="ws://x/", token="t")
    fake = ws or _FakeWs()
    client._ws = fake                                    # type: ignore[assignment]
    client._connected.set()
    client._negotiated_version = version
    return client, fake


async def _exec_frame(ws: _FakeWs) -> dict:
    for _ in range(200):
        for frame in ws.sent:
            if frame["op"] == OpName.EXEC:
                return frame
        await asyncio.sleep(0.01)
    raise AssertionError("the exec request was never sent")


async def _spin() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _cancels(ws: _FakeWs) -> list[dict]:
    return [f for f in ws.sent if f["op"] == OpName.EXEC_CANCEL]


async def test_a_cancelled_exec_tells_the_runtime_to_stop_the_command() -> None:
    client, ws = _client("1.4")
    task = asyncio.create_task(client.exec("sleep 60"))
    exec_req_id = (await _exec_frame(ws))["req_id"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _spin()

    (cancel,) = _cancels(ws)
    assert cancel["args"] == {"target_req_id": exec_req_id}
    assert cancel["req_id"] != exec_req_id, "the control request needs a req_id of its own"


@pytest.mark.parametrize("version", ["1.3", "1.0", "0.0", "", "garbage"])
async def test_it_is_not_sent_to_a_runtime_that_did_not_report_1_4(version: str) -> None:
    """Gated on what the SERVER reported in the handshake: an older runtime would answer EUNSUPPORTED, and "0.0" is a client
    that has never connected."""
    client, ws = _client(version)
    task = asyncio.create_task(client.exec("sleep 60"))
    await _exec_frame(ws)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _spin()

    assert _cancels(ws) == []


@pytest.mark.parametrize("version", ["1.4", "1.10", "2.0"])
async def test_it_is_sent_to_a_runtime_at_or_above_1_4(version: str) -> None:
    """Versions compare as numbers, not as text ("1.10" is above "1.4")."""
    client, ws = _client(version)
    task = asyncio.create_task(client.exec("sleep 60"))
    await _exec_frame(ws)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _spin()

    assert len(_cancels(ws)) == 1


async def test_an_exec_that_finished_is_not_cancelled() -> None:
    client, ws = _client("1.4")
    task = asyncio.create_task(client.exec("true"))
    req_id = (await _exec_frame(ws))["req_id"]
    client._streams[req_id].put_nowait({"event": "exit", "data": {"code": 0}})

    result = await asyncio.wait_for(task, timeout=5.0)
    await _spin()

    assert result.exit_code == 0
    assert _cancels(ws) == []


async def test_an_exec_the_runtime_rejected_is_not_cancelled() -> None:
    client, ws = _client("1.4")
    task = asyncio.create_task(client.exec("true"))
    req_id = (await _exec_frame(ws))["req_id"]
    client._streams[req_id].put_nowait({"ok": False, "error": {"code": "EPROTOCOL", "message": "bad argv"}})

    with pytest.raises(RuntimeOpError):
        await asyncio.wait_for(task, timeout=5.0)
    await _spin()

    assert _cancels(ws) == []


async def test_an_abort_that_ends_the_wait_cancels_the_command_too() -> None:
    """``abort`` ends the wait without an exit event (the exec returns with exit code -1): the command must not be left
    running in the container."""
    client, ws = _client("1.4")
    abort = asyncio.Event()
    task = asyncio.create_task(client.exec("sleep 60", abort=abort))
    exec_req_id = (await _exec_frame(ws))["req_id"]

    abort.set()
    result = await asyncio.wait_for(task, timeout=5.0)
    await _spin()

    assert result.exit_code == -1
    assert [c["args"] for c in _cancels(ws)] == [{"target_req_id": exec_req_id}]


async def test_the_cancel_does_not_wait_for_the_send_and_a_second_cancel_cannot_interrupt_it() -> None:
    """Fire-and-forget from a separate task: with the socket stalled on the control frame, the consumer's cancel still
    completes at once (it never awaits the send), and the send task is held (strong reference) until the client closes."""
    client, ws = _client("1.4", _FakeWs(stall_cancels=True))
    task = asyncio.create_task(client.exec("sleep 60"))
    await _exec_frame(ws)

    task.cancel()
    start = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    assert asyncio.get_running_loop().time() - start < 0.5, "the cancel waited for the exec_cancel send"
    await _spin()

    assert len(client._background_tasks) == 1 and not next(iter(client._background_tasks)).done()
    pending = next(iter(client._background_tasks))
    await client.aclose()
    await asyncio.sleep(0)
    assert pending.done(), "closing the client left the send task running"


async def test_it_is_not_sent_on_a_different_connection_than_the_exec_used() -> None:
    """After a reconnect the runtime-side exec died with its connection, and req_ids are per client: the stale cancel must
    not go out on the new connection."""
    client, old = _client("1.4")
    task = asyncio.create_task(client.exec("sleep 60"))
    await _exec_frame(old)
    new = _FakeWs()
    client._ws = new                                     # type: ignore[assignment]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _spin()

    assert _cancels(old) == [] and _cancels(new) == []


async def test_it_is_not_sent_on_a_closed_connection() -> None:
    client, ws = _client("1.4")
    task = asyncio.create_task(client.exec("sleep 60"))
    await _exec_frame(ws)
    ws.closed = True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _spin()

    assert _cancels(ws) == []


async def test_a_failed_send_of_the_cancel_is_swallowed() -> None:
    """Best effort: the runtime stops the command anyway when the connection closes, so a send that raises must not turn into
    an unhandled task exception."""
    client, ws = _client("1.4")
    task = asyncio.create_task(client.exec("sleep 60"))
    await _exec_frame(ws)

    async def broken_send(text: str) -> None:
        raise ConnectionResetError("gone")

    ws.send_str = broken_send                            # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _spin()
        import gc

        gc.collect()
        await _spin()
    finally:
        loop.set_exception_handler(None)

    assert unhandled == []
    assert not client._background_tasks, "the finished send task was not released"
