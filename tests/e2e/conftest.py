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


# ---------------------------------------------------------------------------
# testconfig + support fixtures (Phase 0 of the SMK coverage plan).
# ---------------------------------------------------------------------------

from tests._support.testconfig import Caps, load_config  # noqa: E402
from tests._support.mock_llm_fixtures import mock_llm  # noqa: E402,F401
from tests._support.mcp_fixtures import (  # noqa: E402,F401
    mcp_http_url,
    mcp_stdio_command,
)
from tests._support.seeding import (  # noqa: E402,F401
    local_workspace,
    scripted_provider,
)


@pytest.fixture(scope="session")
def testcfg() -> dict:
    return load_config()


@pytest.fixture(scope="session")
def caps(testcfg: dict) -> Caps:
    return Caps(testcfg)


# Every /v1 route requires auth. The first register creates the initial user
# (later registers 4xx, ignored); login sets the session cookie on the
# per-test client so the SMK tests are authenticated. Opt-in via the
# `authed_client` fixture rather than autouse, so it does not perturb existing
# e2e modules that manage their own auth.
_E2E_USER = {"username": "e2e", "password": "e2e-password-123"}


@pytest_asyncio.fixture
async def authed_client(client):
    import contextlib

    with contextlib.suppress(Exception):
        await client.post("/v1/auth/register", json=_E2E_USER)
    r = await client.post("/v1/auth/login", json=_E2E_USER)
    assert r.status_code in (200, 204), r.text
    return client
