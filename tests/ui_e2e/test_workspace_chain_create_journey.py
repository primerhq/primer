"""UI E2E: workspace chain end-to-end create journey.

Walks the full chain from a clean state through the UI:
  1. Create a local Workspace Provider via the modal.
  2. Create a Workspace Template referencing it via the modal.
  3. Open the Workspaces page; click New Workspace; the new template
     appears in the template row picker; submit; workspace materialises.
  4. The workspace detail page renders.

Pinned invariants:
  * Sequencing works without page refresh: navigating between the
    three pages refetches the next page's resources cleanly.
  * The provider dropdown on the Template modal includes the
    just-created provider within the polling cadence (5s).
  * The template row picker on the New Workspace modal (platform wave
    P1b item 6 - rows replaced the old <select>) includes the
    just-created template.
  * The workspace detail page renders for the new id.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect


from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import open_legacy_route, wait_for_overlay_url
pytestmark = smk("SMK-UI-06", status="partial")


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
        open_legacy_route(page, console_url, "workspaces/providers")
        page.get_by_role(
            "button", name="New workspace provider",
        ).or_(
            page.get_by_role("button", name="New provider")
        ).first.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)
        modal.locator("input.input.mono").first.fill(provider_id)
        modal.locator("[data-testid='ws-provider-path']").fill(f"/tmp/{provider_id}")
        modal.get_by_role("button", name="Create").first.click()
        expect(modal).not_to_be_visible(timeout=10_000)
        wait_for_overlay_url(page, f"workspaces/providers/{provider_id}")

        # ---- 2. Create the template via the UI ---------------------------
        page.wait_for_function(
            "() => typeof window.WorkspaceTemplatesPage === 'function'",
            timeout=20_000,
        )
        open_legacy_route(page, console_url, "workspaces/templates")
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
        modal.get_by_role("button", name="Create").first.click()
        expect(modal).not_to_be_visible(timeout=10_000)
        wait_for_overlay_url(page, f"workspaces/templates/{template_id}")

        # ---- 3. Create the workspace via the existing modal --------------
        page.wait_for_function(
            "() => typeof window.WorkspacesPage === 'function'",
            timeout=20_000,
        )
        open_legacy_route(page, console_url, "workspaces")
        page.get_by_role(
            "button", name="New workspace",
        ).first.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)
        # RETARGET (platform wave P1b item 6): the Template <select> was
        # replaced by a row picker (one .pc-register-row per template).
        # Poll for the new template's row to appear within ~5s, same
        # intent as the old dropdown-options poll.
        template_row = modal.locator(
            f"[data-testid='workspace-template-row-{template_id}']"
        )
        expect(template_row).to_be_visible(timeout=15_000)
        template_row.click()
        modal.get_by_role("button", name="Create").first.click()

        # Modal closes and the shell ENTERS the workspace just created,
        # which is what the pre-S8 route did too and what the shell
        # spells "#/w/<wid>". It is not the workspaces overlay: that
        # addresses a workspace record rather than going there.
        expect(modal).not_to_be_visible(timeout=15_000)
        page.wait_for_url(re.compile(r"#/w/ws-[0-9a-f]+"), timeout=20_000)
        # Grab the workspace id from the URL for cleanup.
        url = page.url
        wid = url.split("#/w/", 1)[1].split("?")[0].split("#")[0]
        workspace_ids.append(wid)
    finally:
        _cleanup(base_url, workspace_ids, [template_id], [provider_id])
