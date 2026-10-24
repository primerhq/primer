"""UI E2E: workspace chain end-to-end create journey.

Walks the full chain from a clean state through the UI:
  1. Create a local Workspace Provider via the modal.
  2. Open the new-workspace dialog; its "Create a template" action opens
     the template overlay over it; create a Workspace Template on the new
     provider; the overlay closes and hands the template back, selected.
  3. Create; the workspace materialises from the template handed back.
  4. The workspace detail page renders.

Pinned invariants:
  * Sequencing works without page refresh: navigating between the
    three pages refetches the next page's resources cleanly.
  * The template overlay's "Runs on" includes the just-created provider.
  * The new-workspace dialog gets the template it made back, selected
    (platform revamp 08; the lead's ruling on #911), without leaving it.
  * The workspace detail page renders for the new id.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect


from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import default_wid, open_overlay, wait_for_overlay_url
from tests.ui_e2e._studio_helpers import open_provider_catalog
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
        open_provider_catalog(page, console_url, cls="workspace")
        page.get_by_test_id("pc-register-open").click()
        overlay = page.get_by_test_id("pc-register")
        expect(overlay).to_be_visible(timeout=15_000)
        overlay.locator("input.input.mono").first.fill(provider_id)
        overlay.locator("[data-testid='ws-provider-path']").fill(f"/tmp/{provider_id}")
        overlay.get_by_role("button", name="Register").first.click()
        expect(overlay).not_to_be_visible(timeout=10_000)
        wait_for_overlay_url(page, f"workspaces/providers/{provider_id}")

        # ---- 2. Create the template from the new-workspace dialog ----------
        # RETARGET (platform revamp 08; the lead's ruling on #911): the template is made where it is needed. The new-workspace dialog's
        # "Create a template" (a small action beside its template dropdown, always offered) opens the template overlay over the dialog; the
        # template it makes comes back selected, so the person never leaves the workspace they are making. The old template list and its
        # modal (this step's previous route) go with Task 15.
        open_overlay(page, console_url, default_wid(console_url), "new-workspace")
        modal = page.get_by_test_id("nv-overlay:new-workspace")
        picker = modal.get_by_test_id("nv-nw-template")
        expect(picker).to_be_visible(timeout=15_000)
        modal.get_by_test_id("nv-nw-new-template").click()
        overlay = page.get_by_test_id("wt-overlay")
        expect(overlay).to_be_visible(timeout=10_000)
        # "Runs on" must offer the just-created provider.
        overlay.get_by_test_id("ws-template-provider").click()
        overlay.get_by_test_id(f"ws-template-provider:{provider_id}").click()
        expect(overlay.get_by_test_id("wt-local")).to_be_visible()
        overlay.get_by_test_id("ws-template-id").fill(template_id)
        overlay.get_by_test_id("ws-template-description").fill("chain test template")
        overlay.get_by_test_id("wt-save").click()
        expect(overlay).not_to_be_visible(timeout=10_000)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(f"/v1/workspace_templates/{template_id}").status_code == 200

        # ---- 3. Create the workspace from the template handed back ---------
        # The dialog stayed open with the new template selected (the hand-back), so Create makes the workspace from it.
        expect(modal).to_be_visible()
        expect(picker).to_contain_text(template_id, timeout=10_000)
        expect(modal.get_by_test_id("nv-nw-summary")).to_contain_text("chain test template")
        modal.get_by_test_id("nv-nw-create").click()

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
