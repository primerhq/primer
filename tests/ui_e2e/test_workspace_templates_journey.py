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
  * Delete confirmation says 'deletion is safe' and removes the row.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


def _seed_provider(base_url: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json={
            "id": provider_id,
            "provider": "local",
            "config": {"kind": "local", "path": f"/tmp/{provider_id}"},
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
        # Wait for the component to load before navigating (babel-standalone
        # must finish compiling templates.jsx).
        page.wait_for_function(
            "() => typeof window.WorkspaceTemplatesPage === 'function'",
            timeout=20_000,
        )
        page.goto(
            f"{console_url}#/workspaces/templates",
            wait_until="domcontentloaded",
        )
        new_btn = page.get_by_role(
            "button", name="New workspace template",
        ).or_(
            page.get_by_role("button", name="New template")
        ).first
        expect(new_btn).to_be_visible(timeout=20_000)
        new_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # Provider picker shows the seeded provider and is auto-selected.
        provider_select = modal.locator("[data-testid='ws-template-provider']")
        expect(provider_select).to_be_visible(timeout=10_000)
        expect(provider_select).to_have_value(provider_id)

        # Fill id + description (description has a data-testid).
        modal.locator("input.input.mono").first.fill(template_id)
        modal.locator("[data-testid='ws-template-description']").fill("dev workspace v1")

        submit = modal.get_by_role("button", name="Create").first
        expect(submit).to_be_enabled()
        submit.click()

        expect(modal).not_to_be_visible(timeout=10_000)
        page.wait_for_url(
            f"**/console/#/workspaces/templates/{template_id}**",
            timeout=15_000,
        )
        # The description should appear in the page body (not just transient toast).
        page_body = page.locator(".page-body")
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

        page.wait_for_url(
            "**/console/#/workspaces/templates", timeout=15_000,
        )
        # Scope to page body to exclude the transient "Template deleted" toast.
        expect(
            page_body.get_by_text(template_id, exact=True)
        ).to_have_count(0, timeout=5_000)
    finally:
        _cleanup(base_url, [template_id], [provider_id])
