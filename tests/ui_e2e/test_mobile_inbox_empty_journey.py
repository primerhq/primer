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
    expect(sheet).to_contain_text("New session")

    # The request was consumed: leaving the sheet and coming back to Spaces does not open it again.
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=10_000)
    page.get_by_role("tab", name="Inbox").click()
    page.get_by_role("tab", name="Spaces").click()
    expect(page.get_by_test_id("nv-mobile-panel:spaces")).to_be_visible()
    expect(page.get_by_role("dialog")).to_have_count(0)


@pytest.mark.ui_e2e
@pytest.mark.timeout(90)
def test_the_inbox_makes_no_claim_while_it_loads_and_shows_the_error_when_the_fetch_fails_then_recovers(page: Page, console_url: str) -> None:
    """The empty state is a claim about the user's queue; before the first answer, and when the answer is an error, it must not be made."""
    page.set_viewport_size(PHONE)
    answer = {"mode": "hold"}
    held: list = []
    failure = {
        "type": "/errors/service-unavailable", "title": "Service Unavailable", "status": 503, "detail": "The server is starting up.",
    }

    def fail(route) -> None:
        route.fulfill(status=503, content_type="application/problem+json", body=json.dumps(failure))

    def respond(route) -> None:
        if answer["mode"] == "hold":
            held.append(route)                    # left open: the request is in flight until the test answers it
        elif answer["mode"] == "fail":
            fail(route)
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [], "total": 0}))

    page.route("**/v1/yields/pending*", respond)
    page.goto(console_url, wait_until="domcontentloaded")

    # First fetch in flight: nothing is claimed.
    expect(page.get_by_test_id("nv-mob-ib-loading")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-mob-ib-empty")).to_have_count(0)
    expect(page.get_by_test_id("nv-mob-ib-start")).to_have_count(0)
    expect(page.get_by_test_id("nv-mob-ib-count")).to_have_text("")

    # It fails: the error says so and offers Try again; still no empty-state claim.
    answer["mode"] = "fail"
    assert held, "the first fetch never reached the route"
    for route in held:
        fail(route)
    error = page.get_by_test_id("nv-mob-ib-error")
    expect(error).to_be_visible(timeout=20_000)
    expect(error).to_contain_text("The server is starting up.")
    expect(page.get_by_test_id("nv-mob-ib-empty")).to_have_count(0)
    expect(page.get_by_test_id("nv-mob-ib-count")).to_have_text("")

    # The server comes back: Try again loads the queue and only now is it empty.
    answer["mode"] = "ok"
    page.get_by_test_id("nv-mob-ib-retry").click()
    expect(page.get_by_test_id("nv-mob-ib-empty")).to_be_visible(timeout=20_000)
    expect(page.get_by_test_id("nv-mob-ib-error")).to_have_count(0)
    expect(page.get_by_test_id("nv-mob-ib-count")).to_have_text("Nothing waiting on you")
