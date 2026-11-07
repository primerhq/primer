"""RuntimeClient advertises the authoritative shared protocol version by default."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from primer.workspace.runtime.protocol import PROTOCOL_VERSION
from primer.workspace.runtime.runtime_client import RuntimeClient


@pytest.mark.parametrize(
    "override, expected",
    [(None, PROTOCOL_VERSION), ("9.9", "9.9")],
    ids=["shared-default", "explicit-override"],
)
async def test_runtime_client_hello_uses_the_shared_default_or_explicit_override(override, expected):
    kwargs = {} if override is None else {"protocol_version": override}
    client = RuntimeClient(url="ws://runtime/", token="token", **kwargs)
    websocket = SimpleNamespace(
        closed=False,
        send_str=AsyncMock(),
        receive=AsyncMock(return_value=SimpleNamespace(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps({"ok": True, "result": {"protocol": PROTOCOL_VERSION}}),
        )),
        close=AsyncMock(),
    )
    client._session = SimpleNamespace(  # type: ignore[assignment]
        closed=False,
        ws_connect=AsyncMock(return_value=websocket),
        close=AsyncMock(),
    )

    stay_open = asyncio.Event()

    async def wait_until_closed() -> None:
        await stay_open.wait()

    client._receive_loop = wait_until_closed  # type: ignore[method-assign]
    client._heartbeat_loop = wait_until_closed  # type: ignore[method-assign]

    try:
        await client._do_connect()
        assert client._protocol_version == expected
        sent = json.loads(websocket.send_str.await_args.args[0])
        assert sent["args"]["protocol"] == expected
    finally:
        await client.aclose()
        await asyncio.sleep(0)
