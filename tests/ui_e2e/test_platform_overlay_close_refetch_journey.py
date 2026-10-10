"""Platform > Services: closing a service's overlay refreshes the card behind it at once (ADM-16 of the 2026-10-08 admin review).

The grid is polled every 15 s and only its own delete refetched it, so an edit made inside the overlay showed on the card up to 15 s late. The edit is made through the API while the
overlay is open (it stands for any write the overlay makes), the overlay is closed, and the card must show the new description within a few seconds, well inside the poll interval.

Honest limit: the poll can fire by chance inside that window, so on the OLD code this journey fails most runs rather than every run; the unit test of the transition
(``tests/ui/test_platform_overlay_close_refetch.py``) is the deterministic pin.
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


def test_an_edit_made_while_the_overlay_is_open_shows_on_the_card_as_soon_as_it_closes(page, base_url: str, console_url: str, unique_suffix: str) -> None:
    name = f"adm16-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/services", json={"name": name, "description": f"before-{unique_suffix}", "viewer_auth": "console"})
        assert r.status_code == 201, r.text
        service_id = r.json()["id"]
    try:
        _open_platform_page(page, console_url, "services")
        page.get_by_test_id("nv-plat-filter").fill(unique_suffix)
        card = page.get_by_test_id(f"nv-pcard:{service_id}")
        expect(card).to_contain_text(f"before-{unique_suffix}", timeout=15_000)

        # A click on the card's name: the stretched ::after of the Open button covers the whole
        # card, so the click lands on the button (force, since the span itself is covered) and
        # opens the overlay; the card div carries no click of its own (static pin:
        # tests/ui/test_console_clickable_controls_are_focusable.py).
        card.locator(".nv-pcard-name").click(force=True)
        expect(page.get_by_test_id("nv-overlay-body")).to_be_visible(timeout=15_000)

        # A write the overlay would make, made through the API while it is open.
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            row = c.get(f"/v1/services/{service_id}").json()
            row["description"] = f"after-{unique_suffix}"
            assert c.put(f"/v1/services/{service_id}", json=row).status_code == 200

        page.get_by_test_id("nv-overlay-close").click()
        expect(page.get_by_test_id("nv-overlay-body")).to_have_count(0)

        # The poll is 15 s; a refresh caused by the close shows well inside 4 s.
        expect(card).to_contain_text(f"after-{unique_suffix}", timeout=4_000)
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/services/{service_id}")
