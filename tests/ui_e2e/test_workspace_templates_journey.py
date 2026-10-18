"""UI E2E: workspace template create + edit + delete journey.

Seeds a local workspace provider via the API. Creates a template via
the modal, edits its description, then deletes it.

Pinned invariants:
  * The provider picker lists the seeded provider, defaulting to it
    when only one exists.
  * Submit creates the template AND navigates to its detail page.
  * Edit reopens the modal pre-filled with the row's current values
    (description visible in the input).
  * Save (PUT) closes the modal; the detail header re-renders with
    the new description.
  * Delete confirmation says 'deletion is safe' and removes the row; the
    overlay closes and the operator lands on the Platform templates page
    (the address is checked AFTER the delete). That page is a custom page
    (seam I-2, Plan 08), so the journey asserts its frame
    ``nv-plat-page:templates`` and the current sidebar row, with a stand-in
    component loaded until the real page ships; it never asserts what the
    page itself draws.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect


from tests._support.smk import smk  # noqa: E402
from tests.ui_e2e._shell_helpers import (
    install_templates_page_stand_in,
    open_legacy_route,
    wait_for_overlay_url,
)
pytestmark = smk("SMK-UI-06", status="partial")


def _seed_provider(base_url: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": provider_id,
            "provider": "local",
            "config": {"kind": "local", "root_path": f"/tmp/{provider_id}"},
        })
        assert r.status_code == 201, r.text


def _cleanup(base_url: str, template_ids: list[str], provider_ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for tid in template_ids:
            try: c.delete(f"/v1/workspace_templates/{tid}")
            except Exception: pass
        for pid in provider_ids:
            try: c.delete(f"/v1/workspace_providers/{pid}")
            except Exception: pass


def test_workspace_template_create_edit_delete_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    provider_id = f"ws-prov-{unique_suffix}"
    template_id = f"ws-tpl-{unique_suffix}"

    _seed_provider(base_url, provider_id)

    try:
        # The Workspace templates page is a custom page that the Platform view
        # mounts only while ``window.WT_TemplatesPage`` is loaded (Plan 08
        # ships it), and the generic templates page this journey used to land
        # on after the delete is gone (design 14.2). Without a component the
        # delete would land on the Providers fallback, so install the
        # stand-in the old-link journey uses (it reloads the page).
        install_templates_page_stand_in(page)
        # Wait for the component to load before navigating (babel-standalone
        # must finish compiling templates.jsx).
        page.wait_for_function(
            "() => typeof window.WorkspaceTemplatesPage === 'function'",
            timeout=20_000,
        )
        open_legacy_route(page, console_url, "workspaces/templates")
        new_btn = page.get_by_role(
            "button", name="New workspace template",
        ).or_(
            page.get_by_role("button", name="New template")
        ).first
        expect(new_btn).to_be_visible(timeout=20_000)
        new_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # Provider picker offers the seeded provider; select it
        # explicitly (the form auto-selects the first provider, which is
        # not necessarily ours when other rows exist on a shared server).
        provider_select = modal.locator("[data-testid='ws-template-provider']")
        expect(provider_select).to_be_visible(timeout=10_000)
        provider_select.select_option(provider_id)
        expect(provider_select).to_have_value(provider_id)

        # Fill id + description (description has a data-testid).
        modal.locator("input.input.mono").first.fill(template_id)
        modal.locator("[data-testid='ws-template-description']").fill("dev workspace v1")

        submit = modal.get_by_role("button", name="Create").first
        expect(submit).to_be_enabled()
        submit.click()

        expect(modal).not_to_be_visible(timeout=10_000)
        wait_for_overlay_url(page, f"workspaces/templates/{template_id}")
        # The description should appear in the page body (not just transient toast).
        # The overlay body is the successor to .page-body, and it is
        # still the right scope: the toast stack renders at the
        # shell root, outside it.
        page_body = page.get_by_test_id("nv-overlay-body")
        expect(page_body.get_by_text("dev workspace v1", exact=False).first).to_be_visible(
            timeout=10_000
        )

        # Open Edit; pre-filled description.
        page.get_by_role("button", name="Edit", exact=True).first.click()
        edit_modal = page.locator(".modal").first
        expect(edit_modal).to_be_visible(timeout=5_000)
        # On edit, the id field is hidden — description input has its data-testid.
        desc_input = edit_modal.locator("[data-testid='ws-template-description']")
        expect(desc_input).to_have_value("dev workspace v1")
        desc_input.fill("dev workspace v2")
        save_btn = edit_modal.get_by_role("button", name="Save", exact=True).first
        save_btn.click()

        expect(edit_modal).not_to_be_visible(timeout=10_000)
        # Detail header reflects the new description (the resource refetches).
        expect(page_body.get_by_text("dev workspace v2", exact=False).first).to_be_visible(
            timeout=10_000
        )

        # Delete from the detail header.
        page.get_by_role("button", name="Delete", exact=True).first.click()
        confirm_modal = page.locator(".modal").first
        expect(confirm_modal).to_be_visible(timeout=5_000)
        confirm_modal.get_by_role(
            "button", name="Delete template"
        ).first.click()

        # The overlay closes and the operator is on the Platform templates
        # page (revamp design 10): the custom page's FRAME, with its sidebar
        # row current. The frame and the row are what the console draws; what
        # is inside the frame is the stand-in's (until Plan 08), so this never
        # asks for the stand-in's own test id. This step used to wait for the
        # address ``overlay=workspaces:templates``, which is also a PREFIX of
        # the address from before the delete (``overlay=workspaces:templates:
        # <id>``): it passed at once, whatever the delete did, and the
        # delete opened the Workspaces overlay on a record called
        # "templates" ("workspace not found"). Check the address after.
        templates_page = page.get_by_test_id("nv-plat-page:templates")
        expect(templates_page).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("nv-plat-row:templates")).to_have_attribute(
            "aria-current", "page", timeout=5_000,
        )
        expect(page.locator('[data-testid^="nv-overlay:"]')).to_have_count(0)
        expect(page).not_to_have_url(re.compile(r"overlay="), timeout=5_000)
        expect(page).to_have_url(
            re.compile(r"view=platform(:|%3A)templates"), timeout=5_000,
        )
        # The row is gone. Scope the page assertion to the page (the
        # transient "Template deleted" toast renders the id too, outside
        # it), and ask the API as well. The page assertion holds trivially
        # while the stand-in fills the frame; it asserts the real list once
        # Plan 08's page replaces the stand-in. The API check is the proof of
        # the delete in both cases.
        expect(
            templates_page.get_by_text(template_id, exact=True)
        ).to_have_count(0, timeout=5_000)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(
                f"/v1/workspace_templates/{template_id}"
            ).status_code == 404
    finally:
        _cleanup(base_url, [template_id], [provider_id])
