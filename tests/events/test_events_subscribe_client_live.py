"""events_subscribe over a REAL RuntimeClient + in-process runtime server.

Reproduces the live wedge seen on the cluster: with an open
events_subscribe stream, ordinary ops on the same client must still
complete, and the stream must carry file + exec lifecycle frames.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from aiohttp.test_utils import TestServer

from primer.workspace.runtime.runtime_client import RuntimeClient

# asyncio_mode = "auto" in pyproject.toml: async tests need no marker.


@pytest_asyncio.fixture
async def live(tmp_path: Path) -> AsyncIterator[tuple[RuntimeClient, Path]]:
    from primer_runtime.server import build_app

    token = "events-live-token"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)

    app = build_app(token=token, workspace_root=str(workspace_root))
    test_server = TestServer(app)
    await test_server.start_server()
    url = str(test_server.make_url("/")).replace("http://", "ws://")
    client = RuntimeClient(url=url, token=token)
    await client.connect()
    try:
        yield client, workspace_root
    finally:
        await client.aclose()
        await test_server.close()


async def test_ops_still_complete_with_an_open_stream(live):
    client, root = live
    stream = client.events_subscribe(kinds=["file_changed", "exec_started",
                                            "exec_exited"])
    received: list[dict] = []

    async def _consume():
        async for item in stream:
            received.append(item)

    consumer = asyncio.create_task(_consume())
    try:
        await asyncio.sleep(0.3)  # let the subscription open

        # The wedge assertion: a normal op on the SAME client completes.
        result = await asyncio.wait_for(
            client.exec(["/bin/sh", "-c", "echo probe > probe.txt"]),
            timeout=10.0,
        )
        assert result.exit_code == 0

        # The stream saw the exec lifecycle and the file change.
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            kinds = {i.get("kind") for i in received}
            if {"exec_started", "exec_exited"} <= kinds and any(
                i.get("kind") == "file_changed"
                and i.get("path", "").endswith("probe.txt")
                for i in received
            ):
                break
            await asyncio.sleep(0.1)
        kinds = {i.get("kind") for i in received}
        assert {"exec_started", "exec_exited"} <= kinds, received
        assert any(
            i.get("kind") == "file_changed"
            and i.get("path", "").endswith("probe.txt")
            for i in received
        ), received

        # And a second op still completes AFTER stream traffic flowed.
        second = await asyncio.wait_for(
            client.exec(["/bin/sh", "-c", "cat probe.txt"]), timeout=10.0,
        )
        assert second.exit_code == 0
    finally:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
