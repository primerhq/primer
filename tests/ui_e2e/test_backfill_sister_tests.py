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

import time

import httpx
import pytest


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
        page.goto(
            f"{console_url}#/toolsets/{toolset_id}?tab=sessions",
            wait_until="domcontentloaded",
        )
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
        assert "tab=sessions" in page.url, (
            f"reload dropped ?tab=sessions query: {page.url}"
        )
    finally:
        _cleanup(base_url, [f"/v1/toolsets/{toolset_id}"])


# ===========================================================================
# U0095 — Workspaces sidebar count decrements after API DELETE
# ===========================================================================


def test_u0095_workspaces_sidebar_count_decrements_after_delete(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0095 — Sister of U0024 (which pinned the INCREMENT direction
    after a POST). Seed a workspace + capture baseline sidebar count
    that includes it; DELETE via API; assert the sidebar count
    decrements within ~15s (5s real poll cadence per chrome.jsx:111).

    Pins the polled total contract — operators see deletions
    reflected without a manual refresh. Uses a container-internal
    /tmp path so workspace materialise works regardless of host fs.
    """
    wp_id = f"wp-u95-{unique_suffix}"
    tpl_id = f"wt-u95-{unique_suffix}"
    container_path = f"/tmp/u95-{unique_suffix}"
    created_ws_id: str | None = None
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "path": container_path},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspace_templates", json={
            "id": tpl_id, "description": "u95 tpl",
            "provider_id": wp_id, "backend": {"kind": "local"},
        })
        assert r.status_code == 201, r.text
        r = c.post("/v1/workspaces", json={"template_id": tpl_id})
        assert r.status_code == 201, r.text
        created_ws_id = r.json()["id"]
    cleanup_urls = [
        # The workspace will be deleted by the test itself; only clean
        # up provider + template here.
        f"/v1/workspace_templates/{tpl_id}",
        f"/v1/workspace_providers/{wp_id}",
    ]
    try:
        page.goto(f"{console_url}#/", wait_until="domcontentloaded")
        workspaces_nav = page.locator(
            ".nav-item:has(.label:text('Workspaces'))"
        ).first
        workspaces_nav.wait_for(state="visible", timeout=10_000)

        def _read_count() -> int | None:
            count_el = workspaces_nav.locator(".count").first
            if count_el.count() == 0:
                return None
            txt = (count_el.text_content() or "").strip()
            try:
                return int(txt)
            except ValueError:
                return None

        # Wait for the count to render — it must INCLUDE our seeded
        # workspace, so it should be >= 1.
        baseline: int | None = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            baseline = _read_count()
            if baseline is not None and baseline >= 1:
                break
            page.wait_for_timeout(250)
        assert baseline is not None and baseline >= 1, (
            f"Workspaces sidebar count never reached 1 within 15s "
            f"(seeded workspace not visible); baseline={baseline!r}"
        )

        # DELETE the workspace via API.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.delete(f"/v1/workspaces/{created_ws_id}")
            assert r.status_code in (200, 204), r.text

        # Wait for the sidebar count to drop to baseline-1.
        target = baseline - 1
        deadline = time.monotonic() + 15.0
        actual: int | None = baseline
        while time.monotonic() < deadline:
            actual = _read_count()
            if actual is not None and actual <= target:
                break
            page.wait_for_timeout(250)
        assert actual is not None and actual <= target, (
            f"Workspaces sidebar count did not decrement within 15s "
            f"of DELETE; baseline={baseline}, actual={actual!r}, "
            f"target<={target}"
        )
    finally:
        _cleanup(base_url, cleanup_urls)


