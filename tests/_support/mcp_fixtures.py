"""Fixtures that provide MCP mount config for the e2e tests.

Default to the in-repo fixture servers (hermetic). testconfig.mcp.stdio /
testconfig.mcp.http override to point at the user's own MCP servers.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time

import httpx
import pytest

from tests._support.testconfig import load_config


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def mcp_stdio_command() -> list[str]:
    """The stdio command the primer server launches as an MCP toolset.

    Uses ``uv run`` so command[0] is ``uv`` (in mcp_stdio_allowed_commands)
    and the repo venv + PYTHONPATH resolve ``tests._support``.
    """
    cfg = load_config().get("mcp", {}).get("stdio", {})
    if cfg.get("enabled") and cfg.get("command"):
        return list(cfg["command"])
    return ["uv", "run", "python", "-m", "tests._support.mcp.stdio_server"]


@pytest.fixture
def mcp_http_url():
    """Yield the base URL of a running streamable-HTTP MCP server."""
    cfg = load_config().get("mcp", {}).get("http", {})
    if cfg.get("enabled") and cfg.get("url"):
        yield cfg["url"]
        return
    port = _free_port()
    env = dict(os.environ, PRIMER_TEST_MCP_HTTP_HOST="127.0.0.1",
               PRIMER_TEST_MCP_HTTP_PORT=str(port))
    proc = subprocess.Popen(
        ["uv", "run", "python", "-m", "tests._support.mcp.http_server"],
        env=env,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                # the MCP endpoint rejects a bare GET, but a connection refusal
                # means it is not up yet; any HTTP response means it is.
                httpx.get(url, timeout=1.0)
                break
            except httpx.ConnectError:
                time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
