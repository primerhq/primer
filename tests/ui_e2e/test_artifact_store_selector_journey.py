"""Journey: Providers > Artifact Store's "Store new files in" selector sends new files to another store (plan 09, Task 11).

Lead-scheduled: the author wrote it and never ran it. A filesystem store is registered over REST (its root is a path on the SERVER, which creates the
folder and its marker), chosen in the page, which says where new files go, and the choice is what the server stores and what the page shows after a
reload. On a phone (390 px, the mobile shell's ``view=platform:providers:artifact_storage`` link) the selector sits in the catalog's "Defaults" fold
above the cards, shows the stored store and fits the screen. The database store is selected first and, whatever happens, again at the end, and the
filesystem store is deleted: the lane runs serially (``-n0``), so no other journey writes while it is selected. The lane runs with auth disabled, where
the middleware's synthetic admin is the caller, so the page is an admin's.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import boot_mobile_shell, default_wid
from tests.ui_e2e._studio_helpers import open_provider_catalog

PROVIDERS = "/v1/artifact_storage_providers"
ACTIVE = "/v1/artifact_storage_active_config"
DEFAULT = "artifact-storage-default"
PHONE = {"width": 390, "height": 844}


def _active(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.get(ACTIVE)
        assert r.status_code == 200, r.text
        return r.json()["provider_id"]


@pytest.mark.ui_e2e
def test_new_files_can_be_sent_to_another_store_from_the_providers_page(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    store = f"journey-fs-{unique_suffix}"
    root = f"/tmp/primer-ui-e2e-art-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.put(ACTIVE, json={"provider_id": DEFAULT})
        assert r.status_code == 200, r.text
        r = c.post(PROVIDERS, json={"id": store, "provider": "filesystem", "config": {"root": root}})
        assert r.status_code == 201, r.text
    try:
        open_provider_catalog(page, console_url, cls="artifact_storage")
        select = page.get_by_test_id("as-active-store-select")
        expect(select).to_have_value(DEFAULT, timeout=20_000)
        expect(select.locator(f'option[value="{store}"]')).to_have_text(f"{store} (filesystem)", timeout=15_000)

        select.select_option(store)
        expect(page.locator(".toast .title", has_text=f"New files now go to {store}")).to_be_visible(timeout=20_000)
        assert _active(base_url) == store
        expect(select).to_have_value(store)
        expect(select).not_to_have_attribute("aria-busy", "true")

        page.reload()
        open_provider_catalog(page, console_url, cls="artifact_storage")
        expect(page.get_by_test_id("as-active-store-select")).to_have_value(store, timeout=20_000)

        page.set_viewport_size(PHONE)
        boot_mobile_shell(page, f"{console_url}#/w/{default_wid(console_url)}?view=platform:providers:artifact_storage")
        expect(page.get_by_test_id("provider-body-artifact_storage")).to_be_visible(timeout=20_000)
        page.get_by_test_id("pc-extras:artifact_storage").get_by_role("button", name="Defaults").click()
        phone_select = page.get_by_test_id("as-active-store-select")
        expect(phone_select).to_have_value(store, timeout=20_000)
        box = phone_select.bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= PHONE["width"], f"the select overflows a 390 px screen: {box}"
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.put(ACTIVE, json={"provider_id": DEFAULT})
            c.delete(f"{PROVIDERS}/{store}")
