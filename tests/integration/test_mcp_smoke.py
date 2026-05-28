"""Gated smoke tests against real MCP servers.

Both tests are skipped unless the relevant environment variable is set.
They are intentionally minimal -- they just confirm the adapter can
list tools against a real MCP server.

To run the stdio smoke, install a sample MCP server and set::

    export MCP_TEST_STDIO_CMD="npx @modelcontextprotocol/server-everything"

To run the HTTP smoke, point at any reachable streamable-http MCP
endpoint::

    export MCP_TEST_HTTP_URL="http://localhost:8000/mcp"
"""

from __future__ import annotations

import os
import shlex

import pytest

from primer.model.provider import (
    HttpConfig,
    McpConfig,
    StdioConfig,
    TransportType,
)
from primer.toolset.mcp import McpToolsetProvider


_STDIO_CMD = os.environ.get("MCP_TEST_STDIO_CMD")
_HTTP_URL = os.environ.get("MCP_TEST_HTTP_URL")


@pytest.mark.skipif(not _STDIO_CMD, reason="MCP_TEST_STDIO_CMD not set")
async def test_stdio_smoke() -> None:
    cmd = shlex.split(_STDIO_CMD)
    provider = McpToolsetProvider(
        toolset_id="smoke-stdio",
        config=McpConfig(
            transport=TransportType.STDIO,
            config=StdioConfig(command=cmd),
        ),
    )
    try:
        tools = [t async for t in provider.list_tools()]
        assert tools, "real MCP server returned no tools"
    finally:
        await provider.aclose()


@pytest.mark.skipif(not _HTTP_URL, reason="MCP_TEST_HTTP_URL not set")
async def test_http_smoke() -> None:
    provider = McpToolsetProvider(
        toolset_id="smoke-http",
        config=McpConfig(
            transport=TransportType.HTTP,
            config=HttpConfig(url=_HTTP_URL),
        ),
    )
    tools = [t async for t in provider.list_tools()]
    assert tools, "real MCP HTTP server returned no tools"
