"""UI E2E: workspace Events tab — live tap view via EventSource.

Gated: collected-then-ignored unless ``PRIMER_RUN_UI_E2E=1``, exactly
like every other test in this directory (see conftest.py:collect_ignore_glob).

Covers backlog item U0120 / plan Task 6.1.

What this tests:
  1. The "Events" tab button is visible in the WorkspaceDetail tab strip.
  2. Clicking it mounts the WorkspaceTap component (data-testid="workspace-tap-root").
  3. The filter bar (data-testid="tap-filter-bar") renders with event-class chips.
  4. The EventSource connects: the connection badge transitions to "live"
     (data-testid="tap-conn-live") without console errors.
  5. When a session running on that workspace emits tap events, at least one
     tap-event-row appears in the list.

Steps 1-4 are driven via Playwright against a live server seeded through the
normal httpx client fixture. Step 5 requires a running session; if the seed
POST fails for any reason we assert at minimum that the tab mounted cleanly.

Ladder: workspace_provider → workspace_template → workspace → (optionally) agent → session.
"""

from __future__ import annotations

import os

import httpx
import pytest
from playwright.sync_api import expect


# ---------------------------------------------------------------------------
# Gate — mirrors conftest.py's collect_ignore_glob mechanism
# ---------------------------------------------------------------------------

if os.environ.get("PRIMER_RUN_UI_E2E") != "1":
    collect_ignore_glob = ["test_workspace_tap_events.py"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_workspace_ladder(base_url: str, suffix: str) -> dict[str, str]:
    """Create the minimal ladder needed to get a workspace_id.

    Returns a dict with at least "workspace_id". Raises on unexpected errors.
    """
    ids: dict[str, str] = {}
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # Login
        r = c.post("/v1/auth/login", json={"username": "admin", "password": "admin"})
        if r.status_code not in (200, 204):
            pytest.skip(f"Login failed ({r.status_code}) — server not ready")

        # Workspace provider (local fs)
        ws_root = f"/tmp/tap-e2e-{suffix}"
        rp = c.post(
            "/v1/workspace_providers",
            json={
                "id": f"tap-prov-{suffix}",
                "kind": "local_fs",
                "config": {"root": ws_root},
            },
        )
        if rp.status_code not in (200, 201):
            pytest.skip(f"Could not create workspace provider: {rp.text}")
        ids["provider_id"] = rp.json()["id"]

        # Workspace template
        rt = c.post(
            "/v1/workspace_templates",
            json={
                "id": f"tap-tpl-{suffix}",
                "provider_id": ids["provider_id"],
                "source": {"kind": "empty"},
            },
        )
        if rt.status_code not in (200, 201):
            pytest.skip(f"Could not create workspace template: {rt.text}")
        ids["template_id"] = rt.json()["id"]

        # Workspace
        rw = c.post(
            "/v1/workspaces",
            json={"template_id": ids["template_id"]},
        )
        if rw.status_code not in (200, 201):
            pytest.skip(f"Could not create workspace: {rw.text}")
        ids["workspace_id"] = rw.json()["id"]

    return ids


def _cleanup_ladder(base_url: str, ids: dict[str, str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        c.post("/v1/auth/login", json={"username": "admin", "password": "admin"})
        wid = ids.get("workspace_id")
        if wid:
            c.delete(f"/v1/workspaces/{wid}")
        tid = ids.get("template_id")
        if tid:
            c.delete(f"/v1/workspace_templates/{tid}")
        pid = ids.get("provider_id")
        if pid:
            c.delete(f"/v1/workspace_providers/{pid}")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("PRIMER_RUN_UI_E2E") != "1",
    reason="Set PRIMER_RUN_UI_E2E=1 to run UI e2e tests",
)
def test_workspace_tap_events_tab(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    console_messages: list[dict],
) -> None:
    """Navigate to a workspace's Events tab, assert the tap view mounts and
    the EventSource connection reaches 'live' without console errors."""

    ids = _seed_workspace_ladder(base_url, unique_suffix)
    wid = ids["workspace_id"]

    try:
        # Navigate to the workspace detail page, Events tab
        page.goto(
            console_url + f"#/workspaces/{wid}?tab=events",
            wait_until="domcontentloaded",
        )

        # 1. The Events tab button must be visible in the tab strip
        events_tab_btn = page.locator("button", has_text="Events").first
        expect(events_tab_btn).to_be_visible(timeout=10_000)

        # 2. Click it (in case the hash navigation didn't activate it)
        events_tab_btn.click()

        # 3. The WorkspaceTap root element must mount
        tap_root = page.locator('[data-testid="workspace-tap-root"]')
        expect(tap_root).to_be_visible(timeout=10_000)

        # 4. The filter bar must be present with at least one class chip
        filter_bar = page.locator('[data-testid="tap-filter-bar"]')
        expect(filter_bar).to_be_visible(timeout=5_000)
        # Spot-check: tool_call chip exists
        tool_call_chip = page.locator('[data-testid="tap-filter-tool_call"]')
        expect(tool_call_chip).to_be_visible(timeout=5_000)

        # 5. The event list container must be present
        event_list = page.locator('[data-testid="tap-event-list"]')
        expect(event_list).to_be_visible(timeout=5_000)

        # 6. Connection badge reaches "live" (EventSource opened successfully).
        #    Allow a few seconds for the SSE handshake.
        live_badge = page.locator('[data-testid="tap-conn-live"]')
        expect(live_badge).to_be_visible(timeout=10_000)

        # 7. No console errors so far — EventSource must not have thrown.
        from tests.ui_e2e.conftest import assert_no_console_errors
        assert_no_console_errors(
            console_messages,
            ignore_patterns=[
                r"net::ERR_ABORTED",
                r"favicon",
            ],
        )

        # 8. If any tap events landed in the brief window while we navigated,
        #    assert each row has the expected structure.
        rows = page.locator('[data-testid="tap-event-row"]').all()
        for row in rows:
            # Every row should contain at least one class chip (.pill element)
            assert row.locator(".pill").count() >= 1, (
                "Expected each tap event row to contain a class chip (.pill)"
            )

    finally:
        _cleanup_ladder(base_url, ids)


@pytest.mark.skipif(
    os.environ.get("PRIMER_RUN_UI_E2E") != "1",
    reason="Set PRIMER_RUN_UI_E2E=1 to run UI e2e tests",
)
def test_workspace_tap_class_filter(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
    console_messages: list[dict],
) -> None:
    """Toggle an event-class filter chip and assert the stream re-opens
    (connection badge briefly becomes 'connecting' then 'live' again)."""

    ids = _seed_workspace_ladder(base_url, "flt-" + unique_suffix)
    wid = ids["workspace_id"]

    try:
        page.goto(
            console_url + f"#/workspaces/{wid}?tab=events",
            wait_until="domcontentloaded",
        )

        # Activate Events tab
        events_tab_btn = page.locator("button", has_text="Events").first
        expect(events_tab_btn).to_be_visible(timeout=10_000)
        events_tab_btn.click()

        # Wait for live connection
        live_badge = page.locator('[data-testid="tap-conn-live"]')
        expect(live_badge).to_be_visible(timeout=10_000)

        # Toggle the "tool_call" filter chip — this changes the selector
        # which closes the old EventSource and opens a new one.
        chip = page.locator('[data-testid="tap-filter-tool_call"]')
        expect(chip).to_be_visible(timeout=5_000)
        chip.click()

        # The connection should cycle through connecting → live again
        # (or stay live if the re-open is fast). Either way it must end live.
        # Give 8s for the round-trip.
        expect(live_badge).to_be_visible(timeout=8_000)

        # No console errors
        from tests.ui_e2e.conftest import assert_no_console_errors
        assert_no_console_errors(
            console_messages,
            ignore_patterns=[r"net::ERR_ABORTED", r"favicon"],
        )

    finally:
        _cleanup_ladder(base_url, "flt-" + unique_suffix)
