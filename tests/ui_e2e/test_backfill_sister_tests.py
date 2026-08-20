"""Backfill: toolset Sessions tab deep-link + workspaces decrement count + collections empty state.

Covers backlog items:

* U0094 — Toolset detail Sessions tab deep-link survives reload
  (completes the toolset-detail tab-routing trio: U0036 Config +
  U0045 Tools + this Sessions).
* U0095 — Workspaces sidebar count **decrements** within ~15s of an
  API workspace DELETE. Sister of U0024 (which pinned the increment
  direction).
* U0096 — Knowledge → Collections empty state shows "No collections
  yet" + a New CTA (sister of U0038 for the Workspaces empty state).
"""

from __future__ import annotations


import httpx
import pytest
from tests.ui_e2e._shell_helpers import open_legacy_route


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------


def _cleanup(base_url: str, urls: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in urls:
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0094 — Toolset detail Sessions tab deep-link survives reload
# ===========================================================================


def test_u0094_toolset_sessions_tab_deep_link_survives_reload(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0094 — Sister of U0036 (Config) + U0045 (Tools) for the
    toolset-detail Sessions tab. Navigate directly to
    ``#/toolsets/<id>?tab=sessions``, confirm Sessions is the
    aria-selected tab, reload, confirm the URL + selected state
    survive.

    Pins the toolset-detail tab-routing fallback contract
    (toolsets.jsx:324) for the Sessions branch. The page's
    Sessions tab fetches the broad sessions list (not /tools),
    so this test exercises the routing layer independent of any
    MCP transport behaviour.
    """
    toolset_id = f"ts-u94-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/toolsets", json={
            "id": toolset_id,
            "provider": "mcp",
            "config": {
                "transport": "stdio",
                "config": {
                    "command": ["echo", "placeholder"],
                    "args": [],
                    "env": {},
                },
            },
        })
        assert r.status_code == 201, f"seed toolset failed: {r.text}"

    try:
        open_legacy_route(page, console_url, f"toolsets/{toolset_id}", tab="sessions")
        page.locator("h1.page-title").get_by_text(
            toolset_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        sessions_tab = page.get_by_role("tab", name="Sessions").first
        sessions_tab.wait_for(state="visible", timeout=5_000)
        assert sessions_tab.get_attribute("aria-selected") == "true", (
            f"Sessions tab not selected on deep-link nav; "
            f"aria-selected={sessions_tab.get_attribute('aria-selected')!r}"
        )

        page.reload(wait_until="domcontentloaded")
        page.locator("h1.page-title").get_by_text(
            toolset_id, exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        sessions_tab_after = page.get_by_role(
            "tab", name="Sessions",
        ).first
        sessions_tab_after.wait_for(state="visible", timeout=5_000)
        assert sessions_tab_after.get_attribute("aria-selected") == "true", (
            f"Sessions tab lost selected state after reload; "
            f"aria-selected={sessions_tab_after.get_attribute('aria-selected')!r}"
        )
        # The shell states the tab in the overlay target's section slot
        # rather than as a ?tab= query: same deep link, one grammar.
        assert f"overlay=toolsets:sessions:{toolset_id}" in page.url, (
            f"reload dropped the sessions tab: {page.url}"
        )
    finally:
        _cleanup(base_url, [f"/v1/toolsets/{toolset_id}"])


# ===========================================================================
# U0095 — Workspaces sidebar count decrements after API DELETE
# ===========================================================================


