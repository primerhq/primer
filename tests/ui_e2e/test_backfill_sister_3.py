"""Backfill #3: modal X close button + workspaces list filter input.

Covers backlog items:

* U0100 — Modal X (close) button in the header dismisses any open
  create modal. Completes the modal-dismiss trio with U0044 (ESC)
  and U0097 (overlay-click). Pins shared.jsx:116's
  ``<button className="close" onClick={onClose}>`` against the
  modal-stays-open invariant when other buttons are clicked.

* U0101: a list page's filter input narrows the card grid to
  matching ids (sister of U0037 Agents filter + U0046 Sessions filter
  for the third list-page filter input). It pinned the Workspaces
  page's filter; that page is gone (platform revamp 08, Task 14), and
  it pins Platform › Workspace templates' filter (``wt-filter``) now.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect
from tests.ui_e2e._shell_helpers import default_wid, open_view


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
# U0101: the list filter narrows the cards (the Workspace templates page)
# ===========================================================================


def test_u0101_workspace_templates_filter_narrows_grid(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0101: the third list-page filter (sister of U0037 Agents and U0046 Sessions).

    RETARGET (platform revamp 08, Task 14): the Workspaces list page and its filter are gone (the studio rail lists the workspaces), so the
    contract moves to the list that replaced the workspace-shaped list pages, Platform › Workspace templates: two templates with
    discriminating ids ("alpha" / "beta"); typing alpha's id into ``wt-filter`` keeps ``pk-card:<alpha>`` and drops ``pk-card:<beta>``, and
    widening the filter again shows both. The filter is widened to the two templates' shared prefix rather than emptied, so a shared server's
    other templates cannot push either card to another page of the grid.
    """
    wp_id = f"wp-u101-{unique_suffix}"
    tpl_id = f"tpl-u101-{unique_suffix}"
    tpl_alpha = f"{tpl_id}-alpha"
    tpl_beta = f"{tpl_id}-beta"
    cleanup_urls: list[str] = []
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": wp_id, "provider": "local",
            "config": {"kind": "local", "root_path": f"/tmp/u101-{unique_suffix}"},
        })
        assert r.status_code == 201, r.text
        cleanup_urls.append(f"/v1/workspace_providers/{wp_id}")
        for tid in (tpl_alpha, tpl_beta):
            r = c.post("/v1/workspace_templates", json={
                "id": tid, "description": f"u101 {tid}",
                "provider_id": wp_id, "backend": {"kind": "local"},
            })
            assert r.status_code == 201, r.text
            cleanup_urls.insert(0, f"/v1/workspace_templates/{tid}")

    try:
        open_view(page, console_url, default_wid(console_url), "platform:templates")
        expect(page.get_by_test_id("wt-templates-page")).to_be_visible(timeout=20_000)
        filter_input = page.get_by_test_id("wt-filter")

        filter_input.fill(tpl_id)
        for tid in (tpl_alpha, tpl_beta):
            expect(page.get_by_test_id(f"pk-card:{tid}")).to_be_visible(timeout=15_000)

        # alpha's card stays, beta's drops.
        filter_input.fill(tpl_alpha)
        expect(page.get_by_test_id(f"pk-card:{tpl_alpha}")).to_be_visible(timeout=5_000)
        expect(page.get_by_test_id(f"pk-card:{tpl_beta}")).to_have_count(0, timeout=5_000)

        # Widened again: both cards are back.
        filter_input.fill(tpl_id)
        for tid in (tpl_alpha, tpl_beta):
            expect(page.get_by_test_id(f"pk-card:{tid}")).to_be_visible(timeout=5_000)
    finally:
        _cleanup(base_url, cleanup_urls)
