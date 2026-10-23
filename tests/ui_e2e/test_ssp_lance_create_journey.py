"""UI E2E: create a lance-backed SemanticSearchProvider via the console."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import wait_for_overlay_url
from tests.ui_e2e._studio_helpers import open_provider_catalog

pytestmark = smk("SMK-UI-05", status="partial")


def _cleanup(base_url: str, ids: list[str]) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for sid in ids:
            try:
                c.delete(f"/v1/ssp/{sid}")
            except Exception:  # noqa: BLE001
                pass


def test_ssp_lance_create_via_modal_journey(
    page, base_url: str, console_url: str, unique_suffix: str,
) -> None:
    ssp_id = f"ssp-lance-ui-{unique_suffix}"
    lance_path = f"/tmp/lance-ui-{unique_suffix}"

    try:
        # 1. Navigate to the SSP list page.
        open_provider_catalog(page, console_url, cls="ssp")

        # 2. Open register overlay.
        page.get_by_test_id("pc-register-open").click()
        overlay = page.get_by_test_id("pc-register")
        expect(overlay).to_be_visible(timeout=15_000)

        # 3. Picked backend is pgvector - hostname field is visible.
        expect(overlay.get_by_text("hostname", exact=False).first).to_be_visible()

        # 4. Switch backend to lance using label.
        overlay.get_by_label("backend").select_option("lance")

        # 5. Connection section must hide; Filesystem section appears.
        expect(overlay.get_by_text("hostname", exact=False)).to_have_count(0)
        expect(overlay.get_by_text("Filesystem", exact=False).first).to_be_visible()
        expect(overlay.locator("[data-testid='ssp-lance-path']")).to_be_visible()

        # 6. HNSW knobs behind Advanced details.
        overlay.get_by_test_id("ssp-advanced-details").locator("summary").click()
        expect(overlay.get_by_text("HNSW knobs", exact=False).first).to_be_visible()

        # 7. Fill id + path.
        overlay.locator("input.input.mono").nth(0).fill(ssp_id)
        overlay.locator("[data-testid='ssp-lance-path']").fill(lance_path)

        # 8. Submit "Register".
        submit = overlay.get_by_role("button", name="Register").first
        expect(submit).to_be_enabled()
        submit.click()

        # 9. Overlay closes; URL navigates to detail page.
        expect(overlay).not_to_be_visible(timeout=10_000)
        wait_for_overlay_url(page, f"ssp/{ssp_id}")

        # 10. Detail page header shows the path.
        expect(page.get_by_text(lance_path, exact=False).first).to_be_visible(timeout=10_000)
    finally:
        _cleanup(base_url, [ssp_id])
