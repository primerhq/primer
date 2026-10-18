"""Run the mock OAuth and MCP server of ``tests/_support/mock_oauth_mcp.py`` for one test (plan 07, Task 2).

The server runs in the pytest process (uvicorn in a daemon thread) on a socket this module binds itself, so the port is known before the app is built
(the metadata documents name it) and no other process can take it between "find a free port" and "listen". A test imports the fixture it needs::

    from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401

* ``mock_oauth_mcp`` listens on ``127.0.0.1``: for the in-process and live-server (``tests/e2e``) suites, whose primer server is a host process.
* ``mock_oauth_mcp_lan`` listens on ``0.0.0.0`` and advertises the host's LAN address, like ``tests/ui_e2e/conftest.py::mock_llm_lan``: for a primer
  server inside a container, which cannot reach the test process's loopback. Every URL the mock hands out (the metadata, the authorization and token
  endpoints, the resource) names that address, so the browser and the server both follow them.

Both are function-scoped: each test gets fresh state, so a knob one test turns never reaches the next.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from tests._support.mock_oauth_mcp import MockOAuthServer, MockOAuthState, build_mock_oauth_mcp_app

__all__ = ["MockOAuthServer", "mock_oauth_mcp", "mock_oauth_mcp_lan"]

_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 10.0


def _guess_lan_ip() -> str | None:
    """Best-effort LAN IP so a container on the host's network can reach this test process. Never raises: ``None`` means "no route", and the caller falls
    back to loopback (still right for a host-process primer server)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))          # a UDP connect sends nothing: it only asks the kernel which address it would use
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _serve(*, bind_host: str, advertised_host: str) -> Iterator[MockOAuthServer]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((bind_host, 0))
    port = sock.getsockname()[1]
    state = MockOAuthState()
    base_url = f"http://{advertised_host}:{port}"
    app = build_mock_oauth_mcp_app(state, base_url=base_url)
    # lifespan="on": a session manager that fails to start must stop the server, not leave an MCP endpoint that answers 500 to every request.
    config = uvicorn.Config(app, host=bind_host, port=port, log_level="warning", access_log=False, lifespan="on", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True, name="mock-oauth-mcp")
    thread.start()
    try:
        deadline = time.monotonic() + _START_TIMEOUT_S
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("the mock OAuth server stopped while starting")
            if time.monotonic() > deadline:
                raise RuntimeError("the mock OAuth server did not start")
            time.sleep(0.02)
        yield MockOAuthServer.at(state, base_url)
    finally:
        server.should_exit = True
        thread.join(timeout=_STOP_TIMEOUT_S)
        sock.close()


@pytest.fixture
def mock_oauth_mcp() -> Iterator[MockOAuthServer]:
    """A mock OAuth and MCP server on ``127.0.0.1``."""
    yield from _serve(bind_host="127.0.0.1", advertised_host="127.0.0.1")


@pytest.fixture
def mock_oauth_mcp_lan() -> Iterator[MockOAuthServer]:
    """A mock OAuth and MCP server bound to every interface and advertised at the host's LAN address (loopback when there is none)."""
    yield from _serve(bind_host="0.0.0.0", advertised_host=_guess_lan_ip() or "127.0.0.1")
