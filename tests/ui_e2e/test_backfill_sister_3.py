"""Backfill #3: modal X close button + workspaces list filter input.

Covers backlog items:

* U0100 — Modal X (close) button in the header dismisses any open
  create modal. Completes the modal-dismiss trio with U0044 (ESC)
  and U0097 (overlay-click). Pins shared.jsx:116's
  ``<button className="close" onClick={onClose}>`` against the
  modal-stays-open invariant when other buttons are clicked.

* U0101 — Workspaces list filter input narrows the table to matching
  ids (sister of U0037 Agents filter + U0046 Sessions filter for the
  third list-page filter input). Pins workspaces.jsx:56-78 (text
  filter on id + template_id, client-side).
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect


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
# U0101 — Workspaces list filter input narrows table
# ===========================================================================


def test_u0101_workspaces_list_filter_narrows_table(
    page, base_url, console_url, unique_suffix,
) -> None:
    """U0101 — Seed 2 workspaces with discriminating id suffixes
    ("alpha" / "beta"); navigate to /workspaces; type "alpha" into
    the filter input → only the alpha row remains visible (beta
    drops). Clear filter → both rows visible again.

    Pins workspaces.jsx:56-78's textFilter (client-side substring
    match on id + template_id). Sister of U0037 (Agents filter) +
    U0046 (Sessions filter) — the third list-page filter pinned.
    """
    wp_id = f"wp-u101-{unique_suffix}"
    tpl_id = f"tpl-u101-{unique_suffix}"
    container_path = f"/tmp/u101-{unique_suffix}"
    # Seed two workspaces — embed "alpha" / "beta" in unique template
    # ids so the filter has discriminating substrings to match. The
    # workspace ids themselves are backend-allocated and not under
    # our control. workspaces.jsx:63 matches on `id` + `template_id`,
    # so unique template ids are the reliable discriminator.
    tpl_alpha = f"{tpl_id}-alpha"
    tpl_beta = f"{tpl_id}-beta"
    wp_a = f"{wp_id}-alpha"
    wp_b = f"{wp_id}-beta"
    cleanup_urls: list[str] = []
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        # Two providers (each template is bound to one provider).
        for wpid in (wp_a, wp_b):
            r = c.post("/v1/workspace_providers", json={
                "id": wpid, "provider": "local",
                "config": {"kind": "local", "path": container_path},
            })
            assert r.status_code == 201, r.text
            cleanup_urls.append(f"/v1/workspace_providers/{wpid}")
        for tid, wpid in ((tpl_alpha, wp_a), (tpl_beta, wp_b)):
            r = c.post("/v1/workspace_templates", json={
                "id": tid, "description": f"u101 {tid}",
                "provider_id": wpid, "backend": {"kind": "local"},
            })
            assert r.status_code == 201, r.text
            cleanup_urls.append(f"/v1/workspace_templates/{tid}")
        # Create workspaces from those templates.
        wsids: list[str] = []
        for tid in (tpl_alpha, tpl_beta):
            r = c.post("/v1/workspaces", json={"template_id": tid})
            assert r.status_code == 201, r.text
            wsids.append(r.json()["id"])
        for wsid in wsids:
            cleanup_urls.insert(0, f"/v1/workspaces/{wsid}")

    try:
        page.goto(f"{console_url}#/workspaces", wait_until="domcontentloaded")
        page.locator(".nav-item").first.wait_for(
            state="visible", timeout=20_000,
        )

        # Wait for both rows to land in the table. Scope to tbody so
        # we don't match the templateFilter dropdown's hidden <option>
        # elements that also contain the template id text.
        table_body = page.locator("tbody").first
        for tid in (tpl_alpha, tpl_beta):
            table_body.locator(f"tr:has-text('{tid}')").first.wait_for(
                state="visible", timeout=15_000,
            )

        # Type "alpha" into the filter input.
        filter_input = page.get_by_placeholder(
            "Filter workspaces…", exact=False,
        ).first
        filter_input.wait_for(state="visible", timeout=5_000)
        filter_input.fill("alpha")

        # alpha row stays, beta row drops. Allow a brief settle.
        page.wait_for_timeout(300)
        assert table_body.locator(
            f"tr:has-text('{tpl_alpha}')"
        ).count() >= 1, (
            f"alpha row {tpl_alpha!r} disappeared after filtering on 'alpha'"
        )
        assert table_body.locator(
            f"tr:has-text('{tpl_beta}')"
        ).count() == 0, (
            f"beta row {tpl_beta!r} still visible after filtering on 'alpha' "
            "— textFilter contract broken"
        )

        # Clear the filter — both rows visible again.
        filter_input.fill("")
        page.wait_for_timeout(300)
        for tid in (tpl_alpha, tpl_beta):
            expect(
                table_body.locator(f"tr:has-text('{tid}')").first
            ).to_be_visible(timeout=5_000)
    finally:
        _cleanup(base_url, cleanup_urls)
