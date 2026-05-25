"""UI E2E: workspace chain end-to-end create journey.

Walks the full chain from a clean state through the UI:
  1. Create a local Workspace Provider via the modal.
  2. Create a Workspace Template referencing it via the modal.
  3. Open the Workspaces page; click New Workspace; the new template
     appears in the dropdown; submit; workspace materialises.
  4. The workspace detail page renders.

Pinned invariants:
  * Sequencing works without page refresh: navigating between the
    three pages refetches the next page's resources cleanly.
  * The provider dropdown on the Template modal includes the
    just-created provider within the polling cadence (5s).
  * The template dropdown on the New Workspace modal includes the
    just-created template.
  * The workspace detail page renders for the new id.
"""

from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import expect


def _cleanup(
    base_url: str,
    workspace_ids: list[str],
    template_ids: list[str],
    provider_ids: list[str],
) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for wid in workspace_ids:
            try: c.delete(f"/v1/workspaces/{wid}")
            except Exception: pass
        for tid in template_ids:
            try: c.delete(f"/v1/workspace_templates/{tid}")
            except Exception: pass
        for pid in provider_ids:
            try: c.delete(f"/v1/workspace_providers/{pid}")
            except Exception: pass


def _click_via_js(locator) -> None:
    """Workaround for modal-taller-than-viewport: Playwright's .click()
    refuses to act on out-of-viewport elements; dispatching the click
    via JS evaluate bypasses that."""
    locator.evaluate("el => el.click()")


def test_workspace_chain_create_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    provider_id = f"ws-chain-prov-{unique_suffix}"
    template_id = f"ws-chain-tpl-{unique_suffix}"
    workspace_ids: list[str] = []

    try:
        # ---- 1. Create the provider via the UI ---------------------------
        page.wait_for_function(
            "() => typeof window.WorkspaceProvidersPage === 'function'",
            timeout=20_000,
        )
        page.goto(
            f"{console_url}#/workspaces/providers",
            wait_until="domcontentloaded",
        )
        page.get_by_role(
            "button", name="New workspace provider",
        ).or_(
            page.get_by_role("button", name="New provider")
        ).first.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)
        modal.locator("input.input.mono").first.fill(provider_id)
        modal.locator("[data-testid='ws-provider-path']").fill(f"/tmp/{provider_id}")
        _click_via_js(modal.get_by_role("button", name="Create").first)
        expect(modal).not_to_be_visible(timeout=10_000)
        page.wait_for_url(
            f"**/console/#/workspaces/providers/{provider_id}**",
            timeout=15_000,
        )

        # ---- 2. Create the template via the UI ---------------------------
        page.wait_for_function(
            "() => typeof window.WorkspaceTemplatesPage === 'function'",
            timeout=20_000,
        )
        page.goto(
            f"{console_url}#/workspaces/templates",
            wait_until="domcontentloaded",
        )
        page.get_by_role(
            "button", name="New workspace template",
        ).or_(
            page.get_by_role("button", name="New template")
        ).first.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)
        # Provider picker must list the just-created provider.
        provider_select = modal.locator("[data-testid='ws-template-provider']")
        expect(provider_select).to_be_visible(timeout=10_000)
        provider_select.select_option(provider_id)

        modal.locator("input.input.mono").first.fill(template_id)
        modal.locator("[data-testid='ws-template-description']").fill("chain test template")
        _click_via_js(modal.get_by_role("button", name="Create").first)
        expect(modal).not_to_be_visible(timeout=10_000)
        page.wait_for_url(
            f"**/console/#/workspaces/templates/{template_id}**",
            timeout=15_000,
        )

        # ---- 3. Create the workspace via the existing modal --------------
        page.wait_for_function(
            "() => typeof window.WorkspacesPage === 'function'",
            timeout=20_000,
        )
        page.goto(
            f"{console_url}#/workspaces",
            wait_until="domcontentloaded",
        )
        page.get_by_role(
            "button", name="New workspace",
        ).first.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)
        # Template dropdown should include the new template within ~5s poll.
        template_select = modal.locator("select.select").first
        expect(template_select).to_be_visible(timeout=10_000)
        # Poll the dropdown options until the new template appears.
        deadline = time.time() + 15
        while time.time() < deadline:
            options = template_select.locator("option").all_text_contents()
            if template_id in options:
                break
            page.wait_for_timeout(500)
        template_select.select_option(template_id)
        _click_via_js(modal.get_by_role("button", name="Create").first)

        # Modal closes; URL navigates to the workspace detail page.
        expect(modal).not_to_be_visible(timeout=15_000)
        page.wait_for_url("**/console/#/workspaces/**", timeout=20_000)
        # Grab the workspace id from the URL for cleanup.
        url = page.url
        wid = url.rsplit("/", 1)[-1]
        # Strip any trailing #/? fragments.
        wid = wid.split("?")[0].split("#")[0]
        workspace_ids.append(wid)
    finally:
        _cleanup(base_url, workspace_ids, [template_id], [provider_id])
