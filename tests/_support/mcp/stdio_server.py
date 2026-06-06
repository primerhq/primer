"""In-repo MCP fixture server over stdio.

Launched by the primer server as a subprocess MCP toolset. Run with:
    uv run python -m tests._support.mcp.stdio_server
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from tests._support.mcp._tools import register

mcp = FastMCP("primer-test-stdio")
register(mcp)


if __name__ == "__main__":
    mcp.run()  # default transport is stdio
