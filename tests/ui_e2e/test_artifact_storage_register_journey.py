"""Journey: an artifact-storage provider can be registered from Providers > Artifact storage (board ticket 01a1214c).

The Register provider menu read ``GET /v1/artifact_storage_providers/_types``, which was a 404, so it said "No kinds available." and the form could not be opened. Now the menu lists the three backends
(database, filesystem, S3) with what is true of each (only the database one is built: the other two say "not implemented yet: stored, never used"), and a database row can be created and deleted.
"""

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
        page.click('[data-testid="provider-register-toggle"]')
        kinds = page.locator('[data-testid^="provider-register-kind-"]')
        expect(kinds).to_have_count(3, timeout=15_000)
        expect(page.get_by_test_id("provider-register-panel")).not_to_contain_text("No kinds available")
        assert sorted(kinds.nth(i).get_attribute("data-testid") for i in range(3)) == [
            "provider-register-kind-db", "provider-register-kind-filesystem", "provider-register-kind-s3"]

        # the menu says what is true: only the database backend is built, and only the built-in default row is used
        rows = {kind: page.get_by_test_id(f"provider-register-kind-{kind}") for kind in ("db", "filesystem", "s3")}
        expect(rows["db"]).not_to_contain_text("not implemented")
        expect(rows["filesystem"]).to_contain_text("not implemented yet: stored, never used")
        expect(rows["s3"]).to_contain_text("not implemented yet: stored, never used")

        page.click('[data-testid="provider-register-kind-db"]')
        form_locator = page.get_by_test_id("provider-form-artifact_storage_providers")
        form_locator.wait_for(state="visible", timeout=15_000)
        form = '[data-testid="provider-form-artifact_storage_providers"]'
        page.fill(f'{form} [data-field="id"] input', name)
        expect(page.get_by_test_id("provider-form-save")).to_be_enabled()
        page.click('[data-testid="provider-form-save"]')
        page.wait_for_selector(f'[data-testid="provider-card-{name}"]')

        page.click(f'[data-testid="provider-card-delete-{name}"]')
        page.click(f'[data-testid="provider-card-delete-confirm-{name}"]')
        expect(page.get_by_test_id(f"provider-card-{name}")).to_have_count(0, timeout=15_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/artifact_storage_providers/{name}")
