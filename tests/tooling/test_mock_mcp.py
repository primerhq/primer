"""Validate the in-repo stdio MCP fixture server via the real MCP client."""
from __future__ import annotations

import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.asyncio
async def test_stdio_server_lists_and_calls_tools(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tests._support.mcp.stdio_server"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {"echo", "bump"} <= names

            echoed = await session.call_tool("echo", {"text": "ping"})
            assert any("ping" in str(getattr(c, "text", "")) for c in echoed.content)

            marker = tmp_path / "counter.txt"
            r1 = await session.call_tool("bump", {"marker_path": str(marker)})
            assert any("1" in str(getattr(c, "text", "")) for c in r1.content)
            assert marker.read_text(encoding="utf-8") == "1"
            await session.call_tool("bump", {"marker_path": str(marker)})
            assert marker.read_text(encoding="utf-8") == "2"
