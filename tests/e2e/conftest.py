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
async def anon_client(base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """Per-test async HTTP client with NO authentication.

    For tests that exercise the unauthenticated / 401 path or the auth
    flow itself. Most tests should use ``client`` (authenticated).
    """
    async with httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0),
    ) as c:
        yield c


@pytest_asyncio.fixture
async def client(base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """Per-test async HTTP client, AUTHENTICATED by default.

    Every ``/v1`` route is auth-guarded, so the shared client registers
    (idempotent) + logs in the operator user before yielding. Scoped
    per-test so cookie/connection mutations (e.g. a logout in an
    auth-flow test) cannot leak between tests. Tests that need an
    unauthenticated client use the ``anon_client`` fixture instead.
    """
    import contextlib

    async with httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0),
    ) as c:
        with contextlib.suppress(Exception):
            await c.post("/v1/auth/register", json=_E2E_USER)
            await c.post("/v1/auth/login", json=_E2E_USER)
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
    # Back-compat alias: ``client`` is now authenticated by default. Retained
    # so the many modules that request ``authed_client`` keep working.
    return client


# ---------------------------------------------------------------------------
# Shared pgvector SemanticSearchProvider body (2026-09-09).
#
# 14 e2e test modules each carried their own copy-pasted version of this
# POST body, hardcoding hostname=localhost/port=5432/database=primer_e2e/
# username=primer/password=primer with NO env override at all - correct
# only by coincidence on hosts where 5432 happens to be this bringup's own
# postgres. Silently wrong (pointed at a DIFFERENT, unrelated postgres) on
# any host where it isn't, as it is on the dev host this was found on. Same
# hazard class, same day, as the fixes to test_approvals_journey.py and
# test_halfvec_e2e.py - see those commits for the concrete failure mode
# (an InvalidPasswordError against someone else's postgres, reproduced and
# proven live). One test_builtin_toolsets.py instance additionally named
# the wrong DATABASE ("primer_dogfood", which bringup.sh never creates) -
# also covered by using this helper instead.
#
# PRIMER_DB_PORT/PRIMER_DB_USER/PRIMER_DB_PASSWORD are the SAME variables
# scripts/e2e/bringup.sh and docker-compose.yml already read - not a new,
# independent knob. hostname and database are not env-overridable because
# bringup.sh doesn't make them configurable either (it always creates a
# database literally named "primer_e2e", reachable at "localhost" from the
# process running these tests) - overriding them here is for tests that
# deliberately want a DIFFERENT target, not a portability escape hatch.
# ---------------------------------------------------------------------------


def pgvector_ssp_config(
    *,
    hostname: str | None = None,
    port: int | None = None,
    database: str | None = None,
    username: str | None = None,
    password: str | None = None,
    db_schema: str = "public",
) -> dict:
    """The pgvector "config" sub-object alone, for call sites that build
    their own {"id": ..., "provider": "pgvector", "config": ...} shape."""
    return {
        "hostname": hostname or "localhost",
        "port": port if port is not None
        else int(os.environ.get("PRIMER_DB_PORT", "5432")),
        "database": database or "primer_e2e",
        "username": username or os.environ.get("PRIMER_DB_USER", "primer"),
        "password": password or os.environ.get("PRIMER_DB_PASSWORD", "primer"),
        "db_schema": db_schema,
    }


def pgvector_ssp_body(entity_id: str, **config_overrides) -> dict:
    """Full POST /v1/ssp body for a pgvector SemanticSearchProvider pointed
    at the postgres scripts/e2e/bringup.sh actually created for this run."""
    return {
        "id": entity_id,
        "provider": "pgvector",
        "config": pgvector_ssp_config(**config_overrides),
    }
