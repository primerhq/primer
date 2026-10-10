"""The standing sweep's guards, on pages whose answer is known (review of #668, rounds 3 and 4).

The sweep (``test_console_controls_have_names_sweep.py``) looks at a surface only when it is ready, and fails on a surface that is broken. What "broken" looks like in the console varies by page
(a red span, a stuck title, a banner, an empty list that is really a failure), so the sweep also watches the NETWORK: every ``/v1`` response of 500 or more and every request that failed (not
the ones the console aborts itself, and not its event streams) is a note for the surface being looked at, and a look waits until the ``/v1`` requests in flight have answered. These pages make
the requests themselves (a route answers them), so each guard can be seen to fire, and not to fire.

They run on a blank document served at the console's origin (``blank_console``), not on the console itself: the console polls, and a poll that answered 500 in the middle of a test would be a note
the test did not ask for. A route that holds a request is released when the test ends.
"""

from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import Page, Route

from tests._support.smk import smk
from tests.ui_e2e._a11y import Budget, Look, SweepDeadlineExceeded, evaluate_sweep
from tests.ui_e2e._a11y_sweep import Sweep
from tests.ui_e2e._shell_helpers import open_gate

pytestmark = smk("SMK-UI-06", status="partial")

API = "/v1"        # the console's own origin (its CSP allows no other): a route answers the request, or the request fails
BLANK = "<!doctype html><meta charset=utf-8><title>blank</title><main><input aria-label=\"Name\" data-testid=\"first\"></main>"


@pytest.fixture
def held(page: Page):
    """Routes a test holds (a request that is never answered); each is aborted when the test ends, so none outlives it."""
    routes: list[Route] = []
    try:
        yield routes
    finally:
        for route in routes:
            try:
                route.abort()
            except Exception:  # noqa: BLE001 - the page may already be gone
                pass


@pytest.fixture
def blank_console(page: Page, console_url: str) -> Page:
    """The page, at the console's origin, showing a document with no script: nothing polls, so no note can come from anywhere but the test."""
    page.route(re.compile(r".*/console/(\?.*)?$"), lambda route: route.fulfill(status=200, content_type="text/html", body=BLANK))
    open_gate(page, console_url)      # the console's root, without waiting for a shell that this document does not have
    return page


@pytest.fixture
def sweep(blank_console: Page):
    s = Sweep(blank_console, ready_timeout_ms=500, loaded_timeout_s=0.5)
    try:
        yield s
    finally:
        s.close()


def _call(page: Page, path: str) -> None:
    page.evaluate(f"fetch({API + path!r}).catch(() => null)")


@pytest.mark.ui_e2e
def test_a_server_error_the_page_provoked_is_a_note_for_the_surface_looked_at(blank_console: Page, sweep: Sweep) -> None:
    blank_console.route("**/v1/boom", lambda route: route.fulfill(status=503, body="no"))
    _call(blank_console, "/boom")
    blank_console.wait_for_timeout(300)
    sweep.at("surface one", "main")
    assert sweep.notes == ["surface one: GET /v1/boom answered 503"], sweep.notes
    sweep.at("surface two", "main")
    assert sweep.notes == ["surface one: GET /v1/boom answered 503"], "an event is the note of one surface, not of every later one"


@pytest.mark.ui_e2e
def test_a_client_error_is_the_pages_normal_answer_and_not_a_note(blank_console: Page, sweep: Sweep) -> None:
    blank_console.route("**/v1/nothing", lambda route: route.fulfill(status=404, body="{}"))
    _call(blank_console, "/nothing")
    blank_console.wait_for_timeout(300)
    sweep.at("surface", "main")
    assert sweep.notes == []


@pytest.mark.ui_e2e
def test_a_request_that_failed_is_a_note_and_one_that_was_aborted_is_not(blank_console: Page, sweep: Sweep) -> None:
    blank_console.route("**/v1/refused", lambda route: route.abort("connectionrefused"))
    blank_console.route("**/v1/aborted", lambda route: route.abort("aborted"))
    blank_console.evaluate(f"fetch({API + '/refused'!r}).catch(() => null); fetch({API + '/aborted'!r}).catch(() => null)")
    blank_console.wait_for_timeout(300)
    sweep.at("surface", "main")
    assert len(sweep.notes) == 1 and sweep.notes[0].startswith("surface: GET /v1/refused failed (net::ERR_CONNECTION_REFUSED"), sweep.notes


@pytest.mark.ui_e2e
def test_a_problem_that_arrives_after_the_last_look_is_noted_when_the_sweep_closes(blank_console: Page, sweep: Sweep) -> None:
    """N2 of round 4: the requests of the last surface's final moments belong to a note too."""
    sweep.at("last surface", "main")
    blank_console.route("**/v1/late", lambda route: route.fulfill(status=502, body="no"))
    _call(blank_console, "/late")
    blank_console.wait_for_timeout(300)
    assert sweep.notes == []
    sweep.close()
    assert sweep.notes == ["after the last look: GET /v1/late answered 502"], sweep.notes


@pytest.mark.ui_e2e
def test_a_look_waits_for_the_api_requests_in_flight_so_that_what_they_draw_is_looked_at(blank_console: Page, sweep: Sweep) -> None:
    """A form whose fields come from a request (the provider form's kind fields) shows its static first field at once; the look must not be taken before the rest has arrived. The request is
    started WITHOUT being awaited (``void``): the look begins while the client has not yet been told about it, which is the case the wait has to survive."""
    def slow(route: Route) -> None:
        blank_console.wait_for_timeout(700)
        route.fulfill(status=200, body="{}")

    blank_console.route("**/v1/slow", slow)
    blank_console.evaluate(f"void fetch({API + '/slow'!r}).then(() => document.querySelector('main').insertAdjacentHTML('beforeend', '<input aria-label=\"Late\">'))")
    sweep.at("surface", "main")
    assert sweep.looks["surface"].examined == 2, "the look was taken before the answer was drawn"


@pytest.mark.ui_e2e
def test_the_wait_for_requests_in_flight_honours_the_budget(blank_console: Page, held: list[Route]) -> None:
    """A request that is never answered must not hold a look for the whole idle timeout once the budget is spent."""
    blank_console.route("**/v1/never", lambda route: held.append(route))
    sweep = Sweep(blank_console, ready_timeout_ms=500, loaded_timeout_s=0.5, idle_timeout_s=10, budget=Budget(limit=0, short_ms=100))
    try:
        blank_console.evaluate("void fetch('/v1/never').catch(() => null)")        # (started after the sweep listens, so that it is the one waited for)
        started = time.monotonic()
        sweep.at("surface", "main")
        assert time.monotonic() - started < 3, "waited for the idle timeout of 10 s with the budget already spent"
        assert any("still waiting for GET never" in n for n in sweep.notes), sweep.notes
    finally:
        sweep.close()


@pytest.mark.ui_e2e
def test_an_event_stream_that_never_ends_does_not_hold_the_look(blank_console: Page, sweep: Sweep, held: list[Route]) -> None:
    blank_console.route("**/v1/tap", lambda route: held.append(route))      # never answered: an EventSource that stays open
    blank_console.evaluate(f"window.__es = new EventSource({API + '/tap'!r})")
    blank_console.wait_for_timeout(200)
    started = time.monotonic()
    sweep.at("surface", "main")
    assert time.monotonic() - started < 3, "the look waited for the stream"
    assert sweep.notes == []


@pytest.mark.ui_e2e
def test_a_page_marker_that_never_shows_is_a_note_and_the_sweep_goes_on(blank_console: Page, sweep: Sweep) -> None:
    """N2: one broken page must not abort the other hundred surfaces."""
    blank_console.set_content("<main></main>")
    assert sweep.arrive("platform-view", "agents") is False
    assert len(sweep.notes) == 1 and sweep.notes[0].startswith("platform-view agents: never showed ") and "nv-plat-page:agents" in sweep.notes[0]
    blank_console.set_content('<main><div data-testid="nv-sys-page:users"><input aria-label="Name"></div></main>')
    assert sweep.arrive("system-view", "users") is True
    assert len(sweep.notes) == 1


@pytest.mark.ui_e2e
def test_a_probe_that_gives_up_is_a_note_and_an_empty_look_not_the_end_of_the_sweep(sweep: Sweep) -> None:
    """N5 of round 4: ``AxProbe`` raises after its retry when the page keeps changing under it; that surface is noted and the next one is looked at."""
    def gives_up(*_args, **_kwargs):
        raise AssertionError("the page kept changing under the probe: candidate 3 left the page")

    sweep.probe.examine = gives_up
    sweep.at("restless surface", "main")
    assert sweep.looks["restless surface"] == Look()
    assert len(sweep.notes) == 1 and sweep.notes[0].startswith("restless surface: ") and "kept changing" in sweep.notes[0], sweep.notes
    assert sweep.visited == ["restless surface"]


@pytest.mark.ui_e2e
def test_a_surface_that_stays_loading_is_noted_and_the_budget_shortens_the_waits_after_it(blank_console: Page) -> None:
    """N1: the first stuck looks wait their time out; once the budget is spent a stuck look is noted at once."""
    sweep = Sweep(blank_console, ready_timeout_ms=500, loaded_timeout_s=0.6, budget=Budget(limit=1, short_ms=100))
    try:
        blank_console.set_content('<main><div class="spinner"></div><input aria-label="Name"></main>')
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
def test_a_sweep_past_its_deadline_stops_reports_and_leaves_the_page_answering(blank_console: Page) -> None:
    """Round 4, blocker B2: a stuck page and a 2 s deadline. The sweep is bounded from inside: past the deadline no wait is handed out and the next ``at`` raises an ordinary exception, so the test's own
    ``finally`` runs; the findings so far are reported; and the browser still answers afterwards (a signal timeout left it spinning)."""
    budget = Budget(limit=100, short_ms=1000, deadline_s=2)
    sweep = Sweep(blank_console, ready_timeout_ms=500, loaded_timeout_s=30, budget=budget)
    blank_console.set_content('<main><div class="spinner"></div><input aria-label="Name"></main>')
    started = time.monotonic()
    stopped_at = None
    try:
        for i in range(50):
            sweep.at(f"surface {i}", "main")
    except SweepDeadlineExceeded as exc:
        stopped_at = str(exc)
    elapsed = time.monotonic() - started
    try:
        assert stopped_at is not None, "the sweep ran on past its deadline"
        assert elapsed < 12, f"stopped after {elapsed:.1f} s: the 30 s wait for the spinner was not cut at the deadline"
        assert blank_console.evaluate("1 + 1") == 2, "the page answers after the deadline"
        assert any("still loading" in n for n in sweep.notes), sweep.notes
        problems = evaluate_sweep(found=sweep.found, allowlist=[], visited=sweep.visited, expected=[], looks=sweep.looks, floors={}, notes=sweep.notes, page_errors=[], left=[], completed=False)
        assert any("still loading" in p for p in problems), problems
        before = time.monotonic()
        with pytest.raises(SweepDeadlineExceeded):
            sweep.at("one more", "main")
        assert time.monotonic() - before < 1, "past the deadline a look is refused at once"
    finally:
        sweep.close()


@pytest.mark.ui_e2e
def test_requests_of_a_document_that_was_left_are_not_waited_for(blank_console: Page, sweep: Sweep, held: list[Route]) -> None:
    """A page load drops the requests of the page it replaces without reporting them finished (the sweep goes from the desktop shell to the phone shell with ``page.goto``)."""
    blank_console.route("**/v1/never", lambda route: held.append(route))
    blank_console.evaluate("void fetch('/v1/never').catch(() => null)")   # not returned: evaluate would wait for it forever
    blank_console.wait_for_timeout(200)
    blank_console.goto(blank_console.url.split("#", 1)[0] + "?again=1")
    started = time.monotonic()
    sweep.at("after the page load", "main")
    assert time.monotonic() - started < 3, "waited for a request of the document that was left"
    assert not any("still waiting" in n for n in sweep.notes), sweep.notes


@pytest.mark.ui_e2e
def test_a_request_the_new_document_makes_before_it_has_loaded_is_still_waited_for(blank_console: Page, sweep: Sweep, console_url: str, held: list[Route]) -> None:
    """N6 of round 4: the old requests are forgotten when the NAVIGATION starts, not at ``domcontentloaded``: a script at the top of the new document starts a fetch before that event, and it is in flight."""
    blank_console.route(re.compile(r".*/console/\?early=1$"), lambda route: route.fulfill(
        status=200, content_type="text/html", body="<!doctype html><script>fetch('/v1/early').catch(() => null)</script><main><input aria-label=\"Name\"></main>"))
    blank_console.route("**/v1/early", lambda route: held.append(route))
    blank_console.goto(console_url + "?early=1")
    blank_console.wait_for_timeout(200)
    assert [request.url.rsplit("/", 1)[-1] for request in sweep.in_flight] == ["early"], "the early request of the new document was dropped"


@pytest.mark.ui_e2e
def test_a_second_marker_after_the_deadline_stops_the_look_and_does_not_wait_without_a_limit(blank_console: Page) -> None:
    """Round 5, B2-bis (reproduced by the reviewer): past the deadline ``Budget.wait_ms`` returned 0 and Playwright treats ``timeout=0`` as NO timeout, so the next marker waited for ever (here: 6 s, until the
    page inserted it; on a stuck install, until the 900 s thread timeout killed the lane with no report). Now no wait is handed out after the deadline: the look raises, at 2 s, and the page still answers."""
    sweep = Sweep(blank_console, ready_timeout_ms=15_000, loaded_timeout_s=0.5, budget=Budget(limit=100, short_ms=1000, deadline_s=2))
    blank_console.evaluate(
        "setTimeout(() => document.querySelector('main').insertAdjacentHTML('beforeend', '<p id=\"late-one\">one</p>'), 3000);"
        "setTimeout(() => document.querySelector('main').insertAdjacentHTML('beforeend', '<p id=\"late-two\">two</p>'), 6000)"
    )
    started = time.monotonic()
    try:
        with pytest.raises(SweepDeadlineExceeded):
            sweep.at("two late markers", "main", ready=["#late-one", "#late-two"])
        elapsed = time.monotonic() - started
        assert elapsed < 4, f"returned after {elapsed:.1f} s: a wait past the deadline was handed out without a limit"
        assert any("never showed #late-one" in n for n in sweep.notes), sweep.notes
        assert blank_console.evaluate("1 + 1") == 2
    finally:
        sweep.close()


@pytest.mark.ui_e2e
def test_a_request_the_page_starts_after_the_first_idle_check_is_still_waited_for(blank_console: Page, sweep: Sweep) -> None:
    """N1 of round 5: the ``void`` case above catches a wait that reads the in-flight set once only two times in three (the request event races the round trip). This one is deterministic: the fetch
    starts 80 ms in, after the first (empty) check and well before the second one 250 ms later, is held 700 ms, and draws an input when it is answered."""
    def slow(route: Route) -> None:
        blank_console.wait_for_timeout(700)
        route.fulfill(status=200, body="{}")

    blank_console.route("**/v1/slow", slow)
    blank_console.evaluate(
        "setTimeout(() => { void fetch('/v1/slow').then(() => document.querySelector('main').insertAdjacentHTML('beforeend', '<input aria-label=\"Late\">')); }, 80)"
    )
    sweep.at("surface", "main")
    assert sweep.looks["surface"].examined == 2, "the look was taken before the late request was answered"


@pytest.mark.ui_e2e
def test_a_request_the_old_document_starts_while_the_new_one_is_arriving_is_not_waited_for(blank_console: Page, sweep: Sweep, console_url: str, held: list[Route]) -> None:
    """N6 of round 5: the old document is forgotten when the navigation REQUEST starts, but it is alive until the new one commits, and a request it makes in between is never reported finished. When the
    navigation RESPONSE arrives (the commit, as Playwright's own network-idle counts it) everything but the new document's own requests is dropped. The old document's own timer starts the fetch 150 ms
    in, while the route holds the new document back for 500 ms (the page is not driven from inside a route handler: a call from there can wait for the navigation that waits for the handler)."""
    def arriving(route: Route) -> None:
        blank_console.wait_for_timeout(500)
        route.fulfill(status=200, content_type="text/html", body=BLANK)

    blank_console.route("**/v1/late-old", lambda route: held.append(route))
    blank_console.route(re.compile(r".*/console/\?again=1$"), arriving)
    blank_console.evaluate("setTimeout(() => { void fetch('/v1/late-old').catch(() => null); }, 150)")
    blank_console.goto(console_url + "?again=1")
    assert [request.url.rsplit("/", 1)[-1] for request in sweep.in_flight] == [], "the request of the replaced document is still in flight"
    started = time.monotonic()
    sweep.at("after the page load", "main")
    assert time.monotonic() - started < 3, "waited for a request of the document that was replaced"
    assert not any("still waiting" in n for n in sweep.notes), sweep.notes


@pytest.mark.ui_e2e
def test_a_page_that_cannot_be_opened_is_a_note_and_an_empty_look_and_the_sweep_goes_on(sweep: Sweep) -> None:
    """N4 of round 5: ``open_legacy_route`` raises ``AssertionError`` after its own timeouts (twice 45 s); nothing caught it, and the sweep died with the other surfaces unlooked at."""
    def stuck() -> None:
        raise AssertionError("Locator expected to be visible: nv-overlay:agents")

    assert sweep.reach("overlay-page agents", stuck) is False
    assert sweep.looks["overlay-page agents"] == Look() and sweep.visited == ["overlay-page agents"]
    assert len(sweep.notes) == 1 and sweep.notes[0].startswith("overlay-page agents: not looked at, it could not be opened (AssertionError"), sweep.notes
    assert sweep.reach("overlay-page models", lambda: None) is True
    assert "overlay-page models" not in sweep.looks

    def refused() -> None:
        raise BrowserError("net::ERR_CONNECTION_REFUSED")

    assert sweep.reach("overlay-page tools", refused) is False

    def late() -> None:
        raise SweepDeadlineExceeded("past the deadline")

    with pytest.raises(SweepDeadlineExceeded):
        sweep.reach("overlay-page skills", late)


@pytest.mark.ui_e2e
def test_two_overlapping_requests_are_both_waited_for(blank_console: Page, sweep: Sweep) -> None:
    """N4 of round 6: the first request is answered at 100 ms, the second is held 700 ms and draws an input when it is answered. A wait that stops when the set has once been seen empty, or that
    forgets the second request when the first finishes, looks at the page with one input."""
    def first(route: Route) -> None:
        blank_console.wait_for_timeout(100)
        route.fulfill(status=200, body="{}")

    def second(route: Route) -> None:
        blank_console.wait_for_timeout(700)
        route.fulfill(status=200, body="{}")

    blank_console.route("**/v1/first", first)
    blank_console.route("**/v1/second", second)
    blank_console.evaluate(
        "void fetch('/v1/first'); void fetch('/v1/second').then(() => document.querySelector('main').insertAdjacentHTML('beforeend', '<input aria-label=\"Late\">'))"
    )
    sweep.at("surface", "main")
    assert sweep.looks["surface"].examined == 2, "the look was taken before the second request was answered"


@pytest.mark.ui_e2e
def test_a_page_that_cannot_be_opened_spends_the_budget(blank_console: Page) -> None:
    """N4: a navigation that timed out is a stuck look like any other: the waits after enough of them are short."""
    sweep = Sweep(blank_console, budget=Budget(limit=2, short_ms=100))

    def stuck() -> None:
        raise AssertionError("never came up")

    try:
        assert sweep.budget.used == 0
        sweep.reach("one", stuck)
        sweep.reach("two", stuck)
        assert sweep.budget.used == 2 and sweep.budget.exhausted
    finally:
        sweep.close()


@pytest.mark.ui_e2e
def test_the_defaults_of_the_page_are_what_the_budget_has_left(blank_console: Page) -> None:
    """N3 of round 6: a helper's inner ``goto`` or ``get_attribute`` takes Playwright's default of 30 s, which no deadline caps. ``Sweep.bound_defaults`` sets the page's defaults to what the budget
    allows, and ``reach`` does it before it opens anything."""
    from tests.ui_e2e._a11y import SweepDeadlineExceeded

    clock = [1000.0]
    budget = Budget(deadline_s=100, clock=lambda: clock[0])
    sweep = Sweep(blank_console, budget=budget)
    seen: list[float] = []
    blank_console.set_default_timeout = lambda ms: seen.append(ms)          # type: ignore[method-assign]
    blank_console.set_default_navigation_timeout = lambda ms: seen.append(ms)   # type: ignore[method-assign]
    try:
        budget.wait_ms(1)
        clock[0] += 90
        sweep.reach("page", lambda: None)
        assert seen and max(seen) <= 10_000, seen
        clock[0] += 20
        with pytest.raises(SweepDeadlineExceeded):
            sweep.reach("page two", lambda: None)
    finally:
        del blank_console.set_default_timeout, blank_console.set_default_navigation_timeout
        sweep.close()
