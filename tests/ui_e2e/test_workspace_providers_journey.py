"""UI E2E: workspace provider create + detail + delete journey.

Multi-page Playwright journey on the Workspace Providers page.
Creates a local provider via the register overlay, asserts the row appears
in the list and the detail page renders, then deletes via the detail
header.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import wait_for_overlay_url
from tests.ui_e2e._studio_helpers import open_provider_catalog

pytestmark = smk("SMK-UI-06")


def _cleanup(base_url: str, provider_ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for pid in provider_ids:
            try:
                c.delete(f"/v1/workspace_providers/{pid}")
            except Exception:  # noqa: BLE001
                pass


@pytest.mark.flaky(reruns=2, reruns_delay=1)
def test_workspace_provider_create_detail_delete_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    provider_id = f"ws-prov-{unique_suffix}"

    try:
        open_provider_catalog(page, console_url, cls="workspace")
        page.get_by_test_id("provider-body-workspace").wait_for(
            state="visible", timeout=10_000,
        )

        page.get_by_test_id("pc-register-open").click()
        overlay = page.get_by_test_id("pc-register")
        expect(overlay).to_be_visible(timeout=15_000)

        # Default backend is local; path field visible.
        path_input = overlay.locator("[data-testid='ws-provider-path']")
        expect(path_input).to_be_visible()

        # Switch backend to container — path hides, runtime select appears.
        backend_select = overlay.locator("select.select").first
        backend_select.select_option("container")
        expect(path_input).not_to_be_visible()
        expect(overlay.get_by_text("runtime", exact=False).first).to_be_visible()

        # Back to local — path returns.
        backend_select.select_option("local")
        expect(path_input).to_be_visible()

        # Fill id + path; submit.
        overlay.locator("input.input.mono").first.fill(provider_id)
        path_input.fill(f"/tmp/{provider_id}")

        submit = overlay.get_by_role("button", name="Register").first
        expect(submit).to_be_enabled()
        submit.click()

        # Overlay closes; URL navigates to detail page.
        expect(overlay).not_to_be_visible(timeout=10_000)
        wait_for_overlay_url(page, f"workspaces/providers/{provider_id}")

        # Detail header renders with the backend badge + path summary.
        expect(page.get_by_text(provider_id, exact=False).first).to_be_visible(
            timeout=10_000
        )
        expect(page.get_by_text(f"/tmp/{provider_id}", exact=False).first).to_be_visible()

        # TEMPLATES section: empty state.
        expect(page.locator("[data-testid='wp-section-templates']")).to_be_visible(timeout=5_000)
        expect(
            page.get_by_text("No templates bound", exact=False).first
        ).to_be_visible(timeout=5_000)

        # Delete via the detail header.
        page.get_by_role("button", name="Delete", exact=True).first.click()
        confirm_modal = page.locator(".modal").first
        expect(confirm_modal).to_be_visible(timeout=5_000)
        confirm_modal.get_by_role(
            "button", name="Delete provider"
        ).first.click()

        # The provider catalogue owns its panel detail now. Delete returns to
        # the Workspace type page in the same catalogue surface.
        providers_page = page.get_by_test_id("provider-body-workspace")
        expect(providers_page).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("pc-register-open")).to_be_visible()
        expect(page).to_have_url(
            re.compile(r"[?&]overlay=providers(:|%3A)workspace(?:&|$)"),
            timeout=5_000,
        )
        expect(
            providers_page.get_by_text(provider_id, exact=True)
        ).to_have_count(0, timeout=5_000)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            assert c.get(
                f"/v1/workspace_providers/{provider_id}"
            ).status_code == 404
    finally:
        _cleanup(base_url, [provider_id])
