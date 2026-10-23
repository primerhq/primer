"""Journey: an artifact-storage provider can be registered from Providers > Artifact store."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import open_provider_catalog

pytestmark = smk("SMK-UI-06", status="partial")


@pytest.mark.ui_e2e
def test_an_artifact_storage_provider_can_be_registered_and_deleted_from_the_providers_page(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    name = f"journey-art-{unique_suffix}"
    try:
        open_provider_catalog(page, console_url, cls="artifact_storage")
        page.click('[data-testid="pc-register-open"]')
        overlay = page.get_by_test_id("pc-register")
        overlay.wait_for(state="visible", timeout=15_000)

        type_select = overlay.locator('[data-testid="pc-field:type"]')
        expect(type_select).to_be_visible(timeout=15_000)
        options = type_select.locator("option")
        values = [options.nth(i).get_attribute("value") for i in range(options.count())]
        assert "db" in values and "filesystem" in values and "s3" in values

        type_select.select_option("db")
        overlay.locator('[data-testid="pc-field:id"] input, [data-field="id"] input').first.fill(name)
        expect(overlay.get_by_test_id("provider-form-save")).to_be_enabled()
        overlay.get_by_test_id("provider-form-save").click()
        page.wait_for_selector(f'[data-testid="pk-card:artifact_storage:{name}"]')

        page.click(f'[data-testid="pk-card-menu:artifact_storage:{name}"]')
        page.click(f'[data-testid="pc-card-delete:artifact_storage:{name}"]')
        page.click('[data-testid="dialog-confirm"]')
        expect(page.get_by_test_id(f"pk-card:artifact_storage:{name}")).to_have_count(0, timeout=15_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/artifact_storage_providers/{name}")
