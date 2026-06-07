"""Fixtures that provide MCP mount config for the e2e tests.

These ALWAYS resolve to the in-repo fixture MCP servers (echo / bump with a
marker file) so the hermetic MCP journeys (SMK-X-01/X-02, toolset CRUD, the
stdio allowlist) stay deterministic regardless of testconfig. External-MCP
coverage against the operator's configured servers (testconfig.mcp.stdio /
testconfig.mcp.http) lives in tests/e2e/test_smk_mcp.py, which reads
testconfig directly rather than going through these shared fixtures -- a
configured external server (e.g. open-websearch) has no echo/bump tools and
would break the marker-based hermetic assertions if it leaked in here.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time

import httpx
import pytest


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
    and the repo venv + PYTHONPATH resolve ``tests._support``. Always the
    in-repo stdio server (see module docstring).
    """
    return ["uv", "run", "python", "-m", "tests._support.mcp.stdio_server"]


@pytest.fixture
def mcp_http_url():
    """Yield the base URL of a running in-repo streamable-HTTP MCP server."""
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
