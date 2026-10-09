"""The standing sweep's guards, on pages whose answer is known (review of #668, round 3, B2'').

The sweep (``test_console_controls_have_names_sweep.py``) looks at a surface only when it is ready, and fails on a surface that is broken. What "broken" looks like in the console varies by page
(a red span, a stuck title, a banner, an empty list that is really a failure), so the sweep also watches the NETWORK: every ``/v1`` response of 500 or more and every request that failed (not
the ones the console aborts itself, and not its event streams) is a note for the surface being looked at, and a look waits until the ``/v1`` requests in flight have answered. These pages make
the requests themselves (a route answers them), so each guard can be seen to fire, and not to fire.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Page, Route

from tests._support.smk import smk
from tests.ui_e2e._a11y import Budget
from tests.ui_e2e._a11y_sweep import Sweep

pytestmark = smk("SMK-UI-06", status="partial")

API = "/v1"        # the console's own origin (its CSP allows no other): a route answers the request, or the request fails


def _page_that_calls(page: Page, path: str) -> None:
    page.set_content('<main><input aria-label="Name" data-testid="first"></main>')
    page.evaluate(f"fetch({API + path!r}).catch(() => null)")


@pytest.fixture
def sweep(page: Page):
    s = Sweep(page, ready_timeout_ms=500, loaded_timeout_s=0.5)
    try:
        yield s
    finally:
        s.close()


@pytest.mark.ui_e2e
def test_a_server_error_the_page_provoked_is_a_note_for_the_surface_looked_at(page: Page, sweep: Sweep) -> None:
    page.route("**/v1/boom", lambda route: route.fulfill(status=503, body="no"))
    _page_that_calls(page, "/boom")
    page.wait_for_timeout(300)
    sweep.at("surface one", "main")
    assert sweep.notes == ["surface one: GET /v1/boom answered 503"], sweep.notes
    sweep.at("surface two", "main")
    assert sweep.notes == ["surface one: GET /v1/boom answered 503"], "an event is the note of one surface, not of every later one"


@pytest.mark.ui_e2e
def test_a_client_error_is_the_pages_normal_answer_and_not_a_note(page: Page, sweep: Sweep) -> None:
    page.route("**/v1/nothing", lambda route: route.fulfill(status=404, body="{}"))
    _page_that_calls(page, "/nothing")
    page.wait_for_timeout(300)
    sweep.at("surface", "main")
    assert sweep.notes == []


@pytest.mark.ui_e2e
def test_a_request_that_failed_is_a_note_and_one_that_was_aborted_is_not(page: Page, sweep: Sweep) -> None:
    page.route("**/v1/refused", lambda route: route.abort("connectionrefused"))
    page.route("**/v1/aborted", lambda route: route.abort("aborted"))
    page.set_content('<main><input aria-label="Name"></main>')
    page.evaluate(f"fetch({API + '/refused'!r}).catch(() => null); fetch({API + '/aborted'!r}).catch(() => null)")
    page.wait_for_timeout(300)
    sweep.at("surface", "main")
    assert len(sweep.notes) == 1 and sweep.notes[0].startswith("surface: GET /v1/refused failed (net::ERR_CONNECTION_REFUSED"), sweep.notes


@pytest.mark.ui_e2e
def test_a_look_waits_for_the_api_requests_in_flight_so_that_what_they_draw_is_looked_at(page: Page, sweep: Sweep) -> None:
    """A form whose fields come from a request (the provider form's kind fields) shows its static first field at once; the look must not be taken before the rest has arrived."""
    def slow(route: Route) -> None:
        page.wait_for_timeout(700)
        route.fulfill(status=200, body="{}")

    page.route("**/v1/slow", slow)
    page.set_content('<main><input aria-label="Name"></main>')
    page.evaluate(f"fetch({API + '/slow'!r}).then(() => document.querySelector('main').insertAdjacentHTML('beforeend', '<input aria-label=\"Late\">'))")
    sweep.at("surface", "main")
    assert sweep.looks["surface"].examined == 2, "the look was taken before the answer was drawn"


@pytest.mark.ui_e2e
def test_an_event_stream_that_never_ends_does_not_hold_the_look(page: Page, sweep: Sweep) -> None:
    held: list[Route] = []
    page.route("**/v1/tap", lambda route: held.append(route))      # never answered: an EventSource that stays open
    page.set_content('<main><input aria-label="Name"></main>')
    page.evaluate(f"window.__es = new EventSource({API + '/tap'!r})")
    page.wait_for_timeout(200)
    started = time.monotonic()
    sweep.at("surface", "main")
    assert time.monotonic() - started < 3, "the look waited for the stream"
    assert sweep.notes == []


@pytest.mark.ui_e2e
def test_a_page_marker_that_never_shows_is_a_note_and_the_sweep_goes_on(page: Page, sweep: Sweep) -> None:
    """N2: one broken page must not abort the other hundred surfaces."""
    page.set_content("<main></main>")
    assert sweep.arrive("platform-view", "agents") is False
    assert len(sweep.notes) == 1 and sweep.notes[0].startswith("platform-view agents: never showed ") and "nv-plat-page:agents" in sweep.notes[0]
    page.set_content('<main><div data-testid="nv-sys-page:users"><input aria-label="Name"></div></main>')
    assert sweep.arrive("system-view", "users") is True
    assert len(sweep.notes) == 1


@pytest.mark.ui_e2e
def test_a_surface_that_stays_loading_is_noted_and_the_budget_shortens_the_waits_after_it(page: Page) -> None:
    """N1: the first stuck looks wait their time out; once the budget is spent a stuck look is noted at once."""
    sweep = Sweep(page, ready_timeout_ms=500, loaded_timeout_s=0.6, budget=Budget(limit=1, short_ms=100))
    try:
        page.set_content('<main><div class="spinner"></div><input aria-label="Name"></main>')
        started = time.monotonic()
        sweep.at("first", "main")
        first = time.monotonic() - started
        started = time.monotonic()
        sweep.at("second", "main")
        second = time.monotonic() - started
        assert first >= 0.5, "the first stuck look waited its time"
        assert second < first - 0.2, (first, second)
        assert [n.split(":")[0] for n in sweep.notes] == ["first", "second"] and all("still loading" in n for n in sweep.notes), sweep.notes
    finally:
        sweep.close()


@pytest.mark.ui_e2e
def test_requests_of_a_document_that_was_left_are_not_waited_for(page: Page, sweep: Sweep) -> None:
    """A page load drops the requests of the page it replaces without reporting them finished (the sweep goes from the desktop shell to the phone shell with ``page.goto``)."""
    held: list[Route] = []
    page.route("**/v1/never", lambda route: held.append(route))
    page.set_content('<main><input aria-label="Name"></main>')
    page.evaluate("void fetch('/v1/never').catch(() => null)")   # not returned: evaluate would wait for it forever
    page.wait_for_timeout(200)
    page.goto(page.url.split("#", 1)[0] + "?again=1")
    page.set_content('<main><input aria-label="Name"></main>')
    started = time.monotonic()
    sweep.at("after the page load", "main")
    assert time.monotonic() - started < 3, "waited for a request of the document that was left"
    assert not any("still waiting" in n for n in sweep.notes), sweep.notes
