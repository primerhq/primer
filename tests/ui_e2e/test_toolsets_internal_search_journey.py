"""Platform > Toolsets: the Collections card says "Internal search: off" and its chip explains it (revamp 06 Task 6, Review Focus 5).

With no Internal collections config, the ``collections`` row of ``GET /v1/tools`` carries ``internal_search: false`` and the Collections card shows
the chip. A click opens the internal-search dialog and nothing else (the toolset overlay must not open under it); "Open Internal collections" opens
that overlay, and Close leaves the page as it was. On a phone the card's fact sheet says the same. The lane runs auth-disabled, so the user is an
admin and the chip is a button. Lead-scheduled (ui_e2e); the unit pins are ``tests/ui/test_toolsets_internal_search.py`` and
``tests/ui/test_platform_toolsets_cards.py``.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import default_wid, open_mobile_platform_nav, open_view

pytestmark = smk("SMK-UI-03", status="partial")


def _open_platform_page(page, console_url: str, nav: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own
    normalisation to the Studio view), so wait for it and navigate once more if the page is not the Platform one after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id(f"nv-plat-page:{nav}")
    for attempt in (1, 2):
        open_view(page, console_url, default_wid(console_url), f"platform:{nav}")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def _phone(page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})


def _internal_search_off(base_url: str) -> None:
    """No Internal collections config row: the state the chip reports (a previous test may have configured it)."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        status = c.delete("/v1/internal_collections/config").status_code
    assert status in (204, 404), status


def test_the_collections_card_explains_internal_search_and_opens_its_page(page, base_url: str, console_url: str) -> None:
    _internal_search_off(base_url)
    _open_platform_page(page, console_url, "toolsets")

    expect(page.get_by_test_id("pk-card:collections")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("pk-card:search")).to_have_count(0)
    chip = page.get_by_test_id("ts-internal-search-chip")
    expect(chip).to_have_text("Internal search: off")
    chip.click()

    expect(page.get_by_test_id("dialog-message")).to_contain_text("the system collection")
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)  # the chip's click never reaches the card's Open

    page.get_by_test_id("dialog-confirm").click()
    expect(page.get_by_test_id("nv-overlay:internal-collections")).to_be_visible(timeout=15_000)
    assert "overlay=internal-collections" in page.url


def test_close_leaves_the_toolsets_page_as_it_was(page, base_url: str, console_url: str) -> None:
    _internal_search_off(base_url)
    _open_platform_page(page, console_url, "toolsets")

    page.get_by_test_id("ts-internal-search-chip").click()
    expect(page.get_by_test_id("dialog-message")).to_be_visible()
    page.get_by_test_id("dialog-cancel").click()

    expect(page.locator(".modal-overlay")).to_have_count(0)
    expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)
    assert "overlay=" not in page.url
    expect(page.get_by_test_id("pk-card:collections")).to_be_visible()


def test_the_phone_sheet_says_internal_search_is_off(page, base_url: str, console_url: str) -> None:
    _internal_search_off(base_url)
    _phone(page)
    open_mobile_platform_nav(page, console_url, "toolsets")

    page.get_by_test_id("nv-mob-plat-row:collections").click()

    body = page.get_by_test_id("nv-mob-fact-body")
    expect(body).to_contain_text("internal search", timeout=15_000)
    expect(body).to_contain_text("off")
    # The phone draws the card's status too (seam I-8): for an admin it is the same chip, which closes the sheet and opens the dialog.
    body.get_by_test_id("ts-internal-search-chip").click()
    expect(page.get_by_test_id("dialog-message")).to_contain_text("the system collection")
    page.get_by_test_id("dialog-cancel").click()
    expect(page.get_by_test_id("dialog-message")).to_have_count(0)
