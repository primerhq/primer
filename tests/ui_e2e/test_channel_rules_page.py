"""U-CR-01 - the Channel rules editor route loads cleanly.

Mirrors ``tests/ui_e2e/test_console_loads.py``: gated by
``PRIMER_RUN_UI_E2E=1``, uses the ``page`` / ``console_url`` /
``console_messages`` / ``failed_requests`` fixtures from
``tests/ui_e2e/conftest.py``. Navigate to ``#/channels/rules``, wait for
the ``h1.page-title``, assert it reads "Channel rules", and assert no
unexpected console errors via the same ``_assert_clean_console`` helper.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from tests._support.smk import smk

pytestmark = smk("SMK-UI-CR-01")


def test_channel_rules_route_renders_with_zero_console_errors(
    page,
    console_url: str,
    console_messages: list[dict],
    failed_requests: list[dict],
) -> None:
    """Navigate to the channel-rules route, wait for the page-title,
    assert text + no unexpected console errors / fetch failures."""
    page.goto(console_url + "#/channels/rules", wait_until="domcontentloaded")
    title_locator = page.locator("h1.page-title").first
    title_locator.wait_for(state="visible", timeout=10_000)
    assert "Channel rules" in title_locator.inner_text()
    # Let any post-load fetches (provider/trigger lists) settle so a
    # render-time explosion would have surfaced by now.
    page.wait_for_load_state("networkidle", timeout=10_000)

    # By-design 404s: the sidebar polls the IC subsystem config and a
    # 404 there is the documented "subsystem OFF" signal.
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

    _assert_clean_console(
        console_messages,
        ignore_patterns=[
            r"Failed to load resource:.*favicon",
            r"DevTools failed to load source map",
            r"Failed to load resource:.*status of 404",
        ],
    )


def _assert_clean_console(
    messages: list[dict], *, ignore_patterns: Iterable[str] = (),
) -> None:
    """Local copy of conftest.assert_no_console_errors (kept grep-friendly
    for "what counts as a console error"). Behavior is identical."""
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
