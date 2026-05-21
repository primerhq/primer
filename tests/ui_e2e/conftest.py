"""Shared fixtures for the UI e2e test suite.

These tests run against a live ``matrix-app`` container (or ``matrix api
--run-worker``) serving the operator console at ``/console/``. The
fixtures here do NOT start the server — that is the harness's
responsibility (see ``scripts/e2e/ui-bringup.sh``).

Three fixture families:

* ``client`` — ``httpx.AsyncClient`` bound to ``/v1``. Used for **test
  data setup + cleanup**, not for the behavior under test. The UI is
  exercised via Playwright; the API is the fast path for seeding
  fixtures (providers, agents, workspaces) and tearing them down.
* ``page`` — fresh Playwright page in a fresh browser context per test.
  Console-error and CSP-violation tracking is wired up automatically;
  ``assert_no_console_errors`` lets a test assert clean state at any
  point.
* ``artifact_dir`` — per-test directory under
  ``tests/ui_e2e/.state/artifacts/<test_id>/``. Failure hooks dump a
  screenshot + console log + network log here automatically.

Default-skip mechanism mirrors ``tests/e2e/conftest.py``: unless
``MATRIX_RUN_UI_E2E=1`` is set, every test module in this directory is
collected-then-ignored so a casual ``uv run pytest`` from the root
doesn't drag the browser stack in.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, ConsoleMessage, Page


# ---------------------------------------------------------------------------
# Default-skip gate
# ---------------------------------------------------------------------------


if os.environ.get("MATRIX_RUN_UI_E2E") != "1":
    collect_ignore_glob = ["test_*.py"]


# ---------------------------------------------------------------------------
# Base URL + paths
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Where the matrix server is listening.

    Defaults match ``scripts/e2e/ui-bringup.sh`` (and the production
    docker-compose). Override with ``MATRIX_UI_E2E_BASE_URL`` for
    non-standard setups.
    """
    explicit = os.environ.get("MATRIX_UI_E2E_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("MATRIX_E2E_PORT", "8765")
    return f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def base_url() -> str:
    return _base_url()


@pytest.fixture(scope="session")
def console_url(base_url: str) -> str:
    """Operator-console root URL — what a real user opens."""
    return f"{base_url}/console/"


# ---------------------------------------------------------------------------
# httpx client (for test data setup + cleanup, NOT for behavior under test)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """Per-test async HTTP client bound to ``/v1``.

    Mirrors ``tests/e2e/conftest.py:client`` so a test can hop between
    the two suites' style with no surprises. Use this for seeding
    rows (providers, agents, workspaces) and for cleanup DELETEs —
    NOT for the behavior the test is asserting (that's what Playwright
    is for).
    """
    async with httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0),
    ) as c:
        yield c


@pytest.fixture
def unique_suffix() -> str:
    """Short randomised suffix so concurrent tests in the same iteration
    cannot collide on unique constraints."""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Artifact directory (screenshots + logs land here on failure)
# ---------------------------------------------------------------------------


_ARTIFACTS_ROOT = Path(__file__).parent / ".state" / "artifacts"


def _safe_test_id(nodeid: str) -> str:
    """Turn ``tests/ui_e2e/test_foo.py::test_bar[ws-1]`` into a
    filesystem-safe slug. Strips the path, keeps the function + params."""
    leaf = nodeid.rsplit("::", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", leaf)[:120]


@pytest.fixture
def artifact_dir(request: pytest.FixtureRequest) -> Path:
    """Per-test directory under ``.state/artifacts/``.

    Created lazily on first access. Anything written here survives
    teardown — the harness inspects it in Phase 5 of the UI loop.
    """
    d = _ARTIFACTS_ROOT / _safe_test_id(request.node.nodeid)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Playwright wiring — chromium, fresh context per test, no-cache, console
# tracking, automatic artifact capture on failure
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """Override pytest-playwright defaults.

    * ``viewport``: 1366x768 — large enough that modals don't trigger
      the just-fixed scroll-to-footer code path by accident; small
      enough that the "modal scroll under 600px" regression test still
      exercises the path explicitly.
    * ``ignore_https_errors``: True — the dev container is http-only
      today, but operators may proxy through https in the future.
    * ``user_agent``: marked so server logs can grep for ``matrix-ui-e2e``.
    """
    return {
        **browser_context_args,
        "viewport": {"width": 1366, "height": 768},
        "ignore_https_errors": True,
        "user_agent": "Mozilla/5.0 (matrix-ui-e2e) Playwright",
    }


@pytest.fixture
def console_messages() -> list[dict]:
    """Mutable list the ``page`` fixture appends every console message
    into. Tests can read it directly OR call ``assert_no_console_errors``
    to assert the list contains no ``error`` / ``warning`` levels and
    no CSP refusals.
    """
    return []


@pytest.fixture
def failed_requests() -> list[dict]:
    """Mutable list the ``page`` fixture appends every failed network
    request into. A failure is either ``requestfailed`` (network error)
    or a response with status >= 400. The 'Failed to load resource:
    status 404' console messages don't carry the URL, so this is the
    diagnostic channel for fetch failures.
    """
    return []


@pytest.fixture
def page(
    page: "Page",
    console_messages: list[dict],
    failed_requests: list[dict],
    console_url: str,
    artifact_dir: Path,
    request: pytest.FixtureRequest,
) -> "Page":
    """Wraps pytest-playwright's ``page`` fixture to:

    * Subscribe to ``console`` and ``pageerror`` events; every message
      is appended to the test-scoped ``console_messages`` list.
    * Navigate to the console root before yielding. Tests can navigate
      further via ``page.goto(...)``; the initial nav guarantees React
      has bootstrapped (Babel runtime + foundation modules loaded).
    * On test failure, dump a screenshot + console log to
      ``artifact_dir/``. The hook is in
      ``pytest_runtest_makereport`` below.
    """

    def _on_console(msg: "ConsoleMessage") -> None:
        # pytest-playwright exposes ConsoleMessage with .type, .text, .location
        console_messages.append({
            "level": msg.type,
            "text": msg.text,
            "location": dict(msg.location) if msg.location else None,
        })

    def _on_pageerror(err: BaseException) -> None:
        # pageerror events fire on uncaught exceptions; treat as error.
        console_messages.append({
            "level": "pageerror",
            "text": str(err),
            "location": None,
        })

    def _on_requestfailed(req) -> None:
        failed_requests.append({
            "url": req.url,
            "method": req.method,
            "failure": (req.failure or "unknown"),
            "status": None,
        })

    def _on_response(resp) -> None:
        if resp.status >= 400:
            failed_requests.append({
                "url": resp.url,
                "method": resp.request.method,
                "failure": None,
                "status": resp.status,
            })

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    page.on("requestfailed", _on_requestfailed)
    page.on("response", _on_response)
    page.goto(console_url, wait_until="domcontentloaded")
    # Stash on request.node so the post-call hook can find it.
    request.node._matrix_page = page
    request.node._matrix_console = console_messages
    request.node._matrix_failed_requests = failed_requests
    request.node._matrix_artifacts = artifact_dir
    return page


def assert_no_console_errors(
    console_messages: list[dict],
    *,
    ignore_patterns: list[str] | None = None,
) -> None:
    """Assert that no console message at ``error`` level (and no
    ``pageerror``) was recorded. ``ignore_patterns`` is a list of
    regex strings; messages whose ``.text`` matches any pattern are
    excluded from the assertion.

    Common ignores callers may pass:
    * ``r"net::ERR_ABORTED"`` — fetch abort on navigation, harmless.
    * ``r"Failed to load resource:.*favicon"`` — favicon racing.
    """
    ignored = [re.compile(p) for p in (ignore_patterns or [])]
    errors = [
        m for m in console_messages
        if m["level"] in ("error", "pageerror")
        and not any(pat.search(m["text"]) for pat in ignored)
    ]
    assert not errors, (
        f"Expected no console errors, got {len(errors)}:\n"
        + "\n".join(f"  [{m['level']}] {m['text']}" for m in errors)
    )


# Exported as a fixture for ergonomic test code.
@pytest.fixture
def assert_no_console_errors_fn():
    """Returns the ``assert_no_console_errors`` callable so tests can
    call it without re-importing."""
    return assert_no_console_errors


# ---------------------------------------------------------------------------
# Per-test failure capture: screenshot + console + network
# ---------------------------------------------------------------------------


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo,
) -> Iterator[None]:
    """On any test failure during the ``call`` phase, dump artifacts.

    Runs as a wrapper so the report is available when we look at it.
    Only acts on failures so passing tests don't churn the filesystem.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    page = getattr(item, "_matrix_page", None)
    console = getattr(item, "_matrix_console", None)
    artifacts = getattr(item, "_matrix_artifacts", None)
    if page is None or artifacts is None:
        return
    try:
        page.screenshot(path=str(artifacts / "failure.png"), full_page=True)
    except Exception:  # noqa: BLE001 — best-effort
        pass
    if console is not None:
        (artifacts / "console.log").write_text(
            "\n".join(f"[{m['level']}] {m['text']}" for m in console),
            encoding="utf-8",
        )
    failed = getattr(item, "_matrix_failed_requests", None)
    if failed is not None:
        (artifacts / "failed-requests.log").write_text(
            "\n".join(
                f"[{r.get('status') or r.get('failure')}] {r['method']} {r['url']}"
                for r in failed
            ),
            encoding="utf-8",
        )
