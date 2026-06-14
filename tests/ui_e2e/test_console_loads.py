"""U0001 — Every navigable console route loads cleanly.

The simplest possible UI smoke. For each route in the sidebar nav,
navigate to it via hash routing, wait for the page-title to appear,
assert the expected text, and assert zero console errors. This is the
regression net that catches:

* JSX syntax errors that break Babel-standalone transpile
* CSP violations that block React or a script tag
* Routing misconfigurations that 404 a known route
* Missing global components that explode at render

Every test here uses ``page`` from conftest (already navigated to
``/console/`` and tracking console messages).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import pytest


# (hash-fragment path, expected text inside <h1 class="page-title">)
# Mirrors the chrome.jsx sidebar inventory + a few subroutes a user
# would deep-link to. Update this list when the sidebar grows or a
# page is renamed.
_ROUTES: list[tuple[str, str]] = [
    ("#/",                                  "Dashboard"),
    ("#/sessions",                          "Sessions"),
    ("#/workspaces",                        "Workspaces"),
    ("#/agents",                            "Agents"),
    ("#/graphs",                            "Graphs"),
    ("#/knowledge/collections",             "Collections"),
    ("#/knowledge/documents",               "Documents"),
    ("#/toolsets",                          "User toolsets"),
    ("#/toolsets/builtin",                  "Built-in toolsets"),
    ("#/providers/llm",                     "LLM providers"),
    ("#/providers/embedding",               "Embedding providers"),
    ("#/providers/cross_encoder",           "Cross-Encoder providers"),
    ("#/subsystems/internal-collections",   "Internal Collections"),
    ("#/workers",                           "Workers"),
    ("#/health",                            "Health"),
]


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-01")


@pytest.mark.parametrize("hash_path,expected_title", _ROUTES, ids=[r[0] for r in _ROUTES])
def test_route_renders_with_zero_console_errors(
    page,
    console_url: str,
    console_messages: list[dict],
    failed_requests: list[dict],
    hash_path: str,
    expected_title: str,
) -> None:
    """Navigate to ``hash_path``, wait for the page-title, assert text +
    no unexpected console errors / fetch failures. The ``page`` fixture
    already loaded ``/console/`` and React has bootstrapped — this just
    changes the hash and re-asserts."""
    page.goto(console_url + hash_path, wait_until="domcontentloaded")
    title_locator = page.locator("h1.page-title").first
    title_locator.wait_for(state="visible", timeout=10_000)
    assert expected_title in title_locator.inner_text()
    # Give the page a moment for any post-load fetches (sidebar IC poll,
    # per-page list fetch) to settle so failures are caught.
    page.wait_for_load_state("networkidle", timeout=10_000)

    # By-design 404s: the sidebar polls /v1/internal_collections/config
    # and a 404 there is the documented "subsystem OFF" signal (per
    # chrome.jsx and app spec §12). Strip those out before asserting.
    by_design_404_patterns = [
        r"/v1/internal_collections/config",
    ]
    real_failures = [
        r for r in failed_requests
        if not any(re.search(p, r["url"]) for p in by_design_404_patterns)
    ]
    assert not real_failures, (
        "Unexpected fetch failures on route nav:\n"
        + "\n".join(
            f"  [{r.get('status') or r.get('failure')}] {r['method']} {r['url']}"
            for r in real_failures
        )
    )

    # Console errors NOT explained by the by-design 404 (which surfaces
    # as a generic "Failed to load resource: 404" without URL).
    _assert_clean_console(
        console_messages,
        ignore_patterns=[
            r"Failed to load resource:.*favicon",
            r"DevTools failed to load source map",
            # The IC subsystem 404 surfaces here too with no URL —
            # filtered out unconditionally because the network-level
            # check above already proved nothing else 404'd.
            r"Failed to load resource:.*status of 404",
        ],
    )


def _assert_clean_console(
    messages: list[dict], *, ignore_patterns: Iterable[str] = (),
) -> None:
    """Local copy of conftest.assert_no_console_errors so this test
    file is grep-friendly for "what counts as a console error". Behavior
    is identical."""
    import re
    pats = [re.compile(p) for p in ignore_patterns]
    errors = [
        m for m in messages
        if m["level"] in ("error", "pageerror")
        and not any(pat.search(m["text"]) for pat in pats)
    ]
    assert not errors, (
        "Expected no console errors during route nav, got:\n"
        + "\n".join(f"  [{m['level']}] {m['text']}" for m in errors)
    )
