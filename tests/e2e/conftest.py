"""Shared fixtures for the e2e test suite.

These tests run against a live ``primer api --run-worker`` instance that was
started by ``scripts/e2e/bringup.sh``. The fixtures here do NOT start the
server — that is the harness's responsibility, not pytest's.

If the server isn't reachable on import, the whole module errors out, which
is the right behaviour: do not silently skip e2e tests when the environment
is broken.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio


# Default-skip mechanism: unless PRIMER_RUN_E2E=1 is set, all e2e test
# modules in this directory are collected-then-ignored. The harness in
# scripts/e2e/ sets the env var; contributors running `uv run pytest` from
# the root do not, so they never see e2e collection failures.
if os.environ.get("PRIMER_RUN_E2E") != "1":
    collect_ignore_glob = ["test_*.py"]


def _base_url() -> str:
    """Resolve the base URL of the running primer server.

    Defaults match ``scripts/e2e/bringup.sh``. Override via
    ``PRIMER_E2E_BASE_URL`` for unusual setups (different host, port, etc.).
    """
    explicit = os.environ.get("PRIMER_E2E_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("PRIMER_E2E_PORT", "8765")
    return f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def base_url() -> str:
    return _base_url()


@pytest.fixture(scope="session")
def api_prefix() -> str:
    return "/v1"


@pytest_asyncio.fixture
async def client(base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """Per-test async HTTP client.

    Scoped per-test (not per-session) so a test that mutates connection
    state (cookies, follow_redirects override, etc.) cannot pollute its
    neighbours.
    """
    async with httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0),
    ) as c:
        yield c


@pytest.fixture
def unique_suffix() -> str:
    """Short randomised suffix for entity names so concurrent tests in the
    same iteration cannot collide on unique constraints."""
    return uuid.uuid4().hex[:12]
