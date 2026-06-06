"""In-repo MCP fixture server over streamable-HTTP.

Launched as a subprocess by the test fixtures; serves the MCP endpoint at
``/mcp`` on the host/port read from the environment. Run with:
    PRIMER_TEST_MCP_HTTP_HOST=127.0.0.1 PRIMER_TEST_MCP_HTTP_PORT=3333 \
        uv run python -m tests._support.mcp.http_server
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from tests._support.mcp._tools import register

_HOST = os.environ.get("PRIMER_TEST_MCP_HTTP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("PRIMER_TEST_MCP_HTTP_PORT", "3333"))

mcp = FastMCP("primer-test-http", host=_HOST, port=_PORT)
register(mcp)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
