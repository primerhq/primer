"""Journey: the empty phone Inbox offers a way forward (console review C-040, second half).

At 390 px, with nothing waiting on the user, the Inbox says what it is for and offers "Start a session"; tapping it lands on the Spaces tab with
the Create session sheet open. The attention list is answered empty by the test so the lane's other journeys cannot leave a parked session
behind to hide the empty state.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_mobile_shell

PHONE = {"width": 390, "height": 844}


@pytest.mark.ui_e2e
@pytest.mark.timeout(90)
def test_the_empty_inbox_offers_to_start_a_session_and_opens_the_create_sheet(page: Page, console_url: str) -> None:
    page.set_viewport_size(PHONE)
    page.route("**/v1/yields/pending*", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [], "total": 0})))
    open_mobile_shell(page, console_url)

    empty = page.get_by_test_id("nv-mob-ib-empty")
    expect(empty).to_be_visible(timeout=20_000)
    expect(empty).to_contain_text("Nothing needs you right now.")
    expect(empty).to_contain_text("wait here")

    page.get_by_test_id("nv-mob-ib-start").click()

    expect(page.get_by_test_id("nv-mobile-panel:spaces")).to_be_visible(timeout=10_000)
    sheet = page.get_by_role("dialog")
    expect(sheet).to_be_visible(timeout=10_000)
    expect(sheet).to_contain_text("Create session")

    # The request was consumed: leaving the sheet and coming back to Spaces does not open it again.
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)
    page.get_by_role("tab", name="Inbox").click()
    page.get_by_role("tab", name="Spaces").click()
    expect(page.get_by_test_id("nv-mobile-panel:spaces")).to_be_visible()
    expect(page.get_by_role("dialog")).to_have_count(0)
