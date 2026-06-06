"""Run the mock OpenAI server for the e2e session.

The mock runs in the pytest process (uvicorn in a daemon thread); the
separate primer server reaches it over HTTP at the yielded base_url.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from tests._support.mock_llm import ScriptRegistry, build_app


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def mock_llm():
    """Yield (registry, base_url) for the session-long mock OpenAI server."""
    registry = ScriptRegistry()
    port = _free_port()
    config = uvicorn.Config(
        build_app(registry), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:  # pragma: no cover - startup failure
        raise RuntimeError("mock LLM server did not start")
    yield registry, f"http://127.0.0.1:{port}/v1"
    server.should_exit = True
    thread.join(timeout=5)
