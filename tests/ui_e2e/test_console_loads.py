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
from tests.ui_e2e._shell_helpers import open_legacy_route


# (legacy route, expected text in the overlay's own title bar)
# The console has no sidebar and no page routes: every management
# surface is an overlay, titled by the verb that opens it. The routes
# below are what a user deep-links to, translated by the facade.
_ROUTES: list[tuple[str, str]] = [
    ("workspaces",                      "Open Workspaces"),
    ("agents",                          "Open Agents"),
    ("graphs",                          "Open Graphs"),
    ("knowledge/collections",           "Open Collections"),
    ("toolsets",                        "Open Toolsets"),
    ("providers/llm",                   "Open Providers Catalog"),
    ("providers/embedding",             "Open Providers Catalog"),
    ("providers/cross_encoder",         "Open Providers Catalog"),
    # The subsystem now has its own overlay; it used to resolve to the
    # knowledge browser, which is a different surface entirely.
    ("subsystems/internal-collections", "Open Internal Collections"),
    ("workers",                         "Open Workers"),
    ("health",                          "Open Workers"),
]


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-01")


@pytest.mark.parametrize("route,expected_title", _ROUTES, ids=[r[0] for r in _ROUTES])
def test_route_renders_with_zero_console_errors(
    page,
    console_url: str,
    console_messages: list[dict],
    failed_requests: list[dict],
    route: str,
    expected_title: str,
) -> None:
    """Open ``route``'s overlay, assert its title +
    no unexpected console errors / fetch failures. The ``page`` fixture
    already loaded ``/console/`` and React has bootstrapped."""
    open_legacy_route(page, console_url, route)
    title_locator = page.locator(".sh-overlay-title").first
    title_locator.wait_for(state="visible", timeout=10_000)
    assert expected_title in title_locator.inner_text()
    # Give the page a moment for any post-load fetches (sidebar IC poll,
    # per-page list fetch) to settle so failures are caught.
    # NOT networkidle: the shell holds live polling (sessions,
    # attention, files) for as long as it is mounted, so the network
    # is never idle and that wait can only time out. A fixed settle is
    # enough here, since what follows only reads what has already
    # loaded.
    page.wait_for_timeout(1_500)

    # By-design 404s: the sidebar polls /v1/internal_collections/config
    # and a 404 there is the documented "subsystem OFF" signal (per
    # the console shell and app spec §12). Strip those out before asserting.
    by_design_404_patterns = [
        r"/v1/internal_collections/config",
    ]
    real_failures = [
        r for r in failed_requests
        if not any(re.search(p, r["url"]) for p in by_design_404_patterns)
        # net::ERR_ABORTED is a fetch cancelled by navigation / useResource
        # cleanup (a route change aborts the prior route's in-flight mount
        # fetches). It is harmless (the conftest documents it as such), not a
        # server/route failure — exclude it so route timing doesn't flake this.
        and "ERR_ABORTED" not in (r.get("failure") or "")
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
