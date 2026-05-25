"""UI E2E: workspace provider create + detail + delete journey.

Multi-page Playwright journey on the new Workspace Providers page.
Creates a local provider via the modal, asserts the row appears in
the list and the detail page renders, then deletes via the detail
header.

Pinned invariants:
  * The empty state renders with the call-to-action.
  * The create modal's backend select discriminates the form
    (local shows `path`; container hides it and shows runtime fields).
  * Submit closes the modal AND navigates to the detail page.
  * The Templates tab renders an empty state when nothing references
    the provider.
  * Delete from the detail page returns the operator to the list and
    the row is gone.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


def _cleanup(base_url: str, provider_ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for pid in provider_ids:
            try:
                c.delete(f"/v1/workspace_providers/{pid}")
            except Exception:  # noqa: BLE001
                pass


def test_workspace_provider_create_detail_delete_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    provider_id = f"ws-prov-{unique_suffix}"

    try:
        # The conftest page fixture already navigated to console_url.
        # Wait for the sidebar (.nav-item) to confirm React + all scripts
        # have booted — the workspace provider components use
        # data-type="module" in index.html, so they load asynchronously;
        # we must wait until window.WorkspaceProvidersPage is defined
        # before pushing the hash, otherwise React crashes with
        # "type is invalid — got undefined".
        page.locator(".nav-item").first.wait_for(state="visible", timeout=20_000)
        page.wait_for_function(
            "() => typeof window.WorkspaceProvidersPage === 'function'",
            timeout=15_000,
        )

        # Hash-navigate to the providers page (no full page reload).
        page.evaluate("() => { window.location.hash = '#/workspaces/providers'; }")
        # Wait for the page header to reflect the new route.
        page.locator("h1.page-title").get_by_text(
            "Workspace providers", exact=False,
        ).first.wait_for(state="visible", timeout=10_000)

        # Empty-state CTA OR filter-bar "New provider" — both work.
        new_btn = page.get_by_role(
            "button", name="New workspace provider",
        ).or_(
            page.get_by_role("button", name="New provider")
        ).first
        expect(new_btn).to_be_visible(timeout=20_000)
        new_btn.click()

        modal = page.locator(".modal").first
        expect(modal).to_be_visible(timeout=5_000)

        # Default backend is local; path field visible.
        path_input = modal.locator("[data-testid='ws-provider-path']")
        expect(path_input).to_be_visible()

        # Switch backend to container — path hides, runtime select appears.
        backend_select = modal.locator("select.select").first
        backend_select.select_option("container")
        expect(path_input).not_to_be_visible()
        expect(modal.get_by_text("runtime", exact=False).first).to_be_visible()

        # Back to local — path returns.
        backend_select.select_option("local")
        expect(path_input).to_be_visible()

        # Fill id + path; submit.
        # id field is the first input.input.mono in the modal.
        modal.locator("input.input.mono").first.fill(provider_id)
        path_input.fill(f"/tmp/{provider_id}")

        submit = modal.get_by_role("button", name="Create").first
        expect(submit).to_be_enabled()
        submit.click()

        # Modal closes; URL navigates to detail page.
        expect(modal).not_to_be_visible(timeout=10_000)
        page.wait_for_url(
            f"**/console/#/workspaces/providers/{provider_id}**",
            timeout=15_000,
        )

        # Detail header renders with the backend badge + path summary.
        expect(page.get_by_text(provider_id, exact=False).first).to_be_visible(
            timeout=10_000
        )
        expect(page.get_by_text(f"/tmp/{provider_id}", exact=False).first).to_be_visible()

        # Switch to Templates tab — empty state.
        page.get_by_role("button", name="Templates", exact=False).first.click()
        expect(
            page.get_by_text("No templates bound", exact=False).first
        ).to_be_visible(timeout=5_000)

        # Delete via the detail header. The Delete button is in the panel header.
        page.get_by_role("button", name="Delete", exact=True).first.click()
        # Confirmation modal opens. The "Delete provider" button is the danger
        # action; click it.
        confirm_modal = page.locator(".modal").first
        expect(confirm_modal).to_be_visible(timeout=5_000)
        confirm_modal.get_by_role(
            "button", name="Delete provider"
        ).first.click()

        # URL navigates back to /workspaces/providers and the row is gone.
        page.wait_for_url(
            "**/console/#/workspaces/providers", timeout=15_000,
        )
        # Scope the assertion to the page body (table/empty state) to avoid
        # matching the transient "Provider deleted" toast which also renders
        # the provider id in its detail field.
        page_body = page.locator(".page-body")
        expect(
            page_body.get_by_text(provider_id, exact=True)
        ).to_have_count(0, timeout=5_000)
    finally:
        _cleanup(base_url, [provider_id])
