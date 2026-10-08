"""Platform > Collections: "New collection" opens the dialog on the page, not a second list (ADM-06 of the 2026-10-08 admin review).

The press used to open the LEGACY Collections list overlay (a table with its own "New collection" button) and only a second press opened the dialog. The
page now hosts the dialog the legacy list used. These journeys drive the real Platform page: no management overlay is mounted while the dialog is open,
Cancel returns to the card grid with the address bar untouched, and a created collection lands on its own detail overlay with its card in the grid behind it.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _open_platform_page(page, console_url: str, nav: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own
    normalisation to the Studio view), so wait for it and navigate once more if the page is not the Platform one after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id(f"nv-plat-page:{nav}")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", f"platform:{nav}")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def test_new_collection_opens_the_dialog_with_no_list_behind_it_and_lands_on_the_new_collection(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    collection_id = f"adm06-{unique_suffix}"
    try:
        _open_platform_page(page, console_url, "collections")

        page.get_by_test_id("nv-plat-create").click()

        dialog = page.locator(".modal-overlay")
        expect(dialog).to_contain_text("New collection", timeout=10_000)
        expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
        assert "overlay=" not in page.url, page.url

        dialog.get_by_placeholder("id (optional)").fill(collection_id)
        dialog.get_by_placeholder("Description").fill("adm06 journey")
        dialog.get_by_role("button", name="Create", exact=True).click()

        # The created collection lands on its own detail, and its card is in the grid behind it.
        expect(page.get_by_test_id("nv-overlay-body")).to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("nv-overlay-body")).to_contain_text(collection_id)
        assert f"overlay=collections::{collection_id}" in page.url.replace("%3A", ":"), page.url
        page.get_by_test_id("nv-plat-filter").fill(collection_id)
        expect(page.get_by_test_id(f"nv-pcard-del:{collection_id}")).to_be_attached(timeout=15_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/collections/{collection_id}")


def test_cancelling_the_new_collection_dialog_leaves_the_card_grid_and_the_url_alone(page, console_url: str) -> None:
    _open_platform_page(page, console_url, "collections")
    before = page.url

    page.get_by_test_id("nv-plat-create").click()
    dialog = page.locator(".modal-overlay")
    expect(dialog).to_contain_text("New collection", timeout=10_000)
    dialog.get_by_role("button", name="Cancel").click()

    expect(page.locator(".modal-overlay")).to_have_count(0)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert page.url == before, (before, page.url)
