"""The recorder of the standing a11y sweep: what it waits for, what it notes, and what it counts, for one browser page (console review C-003).

``Sweep`` is in a module of its own, not in the sweep's test file, so that the known-answer tests can drive it on pages they build (``test_a11y_sweep_guards_known_answers.py``): a test module is not
a library (``tests/ui/test_ui_e2e_modules_import_what_exists.py``). For each surface it is handed:

* ``arrive(kind, name)`` waits for the surface's OWN page marker (``ready_selectors``); a marker that never shows is a note and ``False``, not an exception, because one broken page must not abort the
  other hundred surfaces;
* ``at(surface, root)`` waits for the ``/v1`` requests in flight to answer (an event stream is not one), for what is loading under the root to stop, takes the look (``AxProbe.examine`` with the
  fixed ``CHROME`` left out of the body count) and records it. Anything the page said about itself is a NOTE for the surface: an error banner or a loading state it recognises (``page_state``), and,
  independent of how the page draws failure, every ``/v1`` response of 500 or more and every request that failed since the previous look (``api_problem``).

Every wait is bounded, and the bound is shared: ``Budget`` shortens the waits once enough looks have used theirs up, so a broken install is reported and not waited out, and past its deadline it hands
out no wait at all (it raises). ``reach(surface, open_it)`` is how a page is opened: a navigation that times out is a note and an empty look for that page, not the end of the sweep.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import Page, expect

from tests.ui_e2e._a11y import AxProbe, Budget, Look, api_problem, is_event_stream, page_state
from tests.ui_e2e._a11y_surfaces import CHROME, ready_selectors

READY_TIMEOUT_MS = 15_000      # a surface has this long to show a marker
LOADED_TIMEOUT_S = 10          # ... and to stop saying it is loading
IDLE_TIMEOUT_S = 10            # ... and its /v1 requests to answer


class Sweep:
    def __init__(self, page: Page, *, ready_timeout_ms: int = READY_TIMEOUT_MS, loaded_timeout_s: float = LOADED_TIMEOUT_S, idle_timeout_s: float = IDLE_TIMEOUT_S,
                 budget: Budget | None = None) -> None:
        self.page = page
        self.probe = AxProbe(page)
        self.ready_timeout_ms = ready_timeout_ms
        self.loaded_timeout_s = loaded_timeout_s
        self.idle_timeout_s = idle_timeout_s
        self.budget = budget or Budget()
        self.found: dict[str, list[str]] = {}
        self.visited: list[str] = []
        self.looks: dict[str, Look] = {}
        self.notes: list[str] = []
        self.network: list[str] = []
        self._reported = 0
        self._inflight: set = set()
        self._closed = False
        self._listeners = [("request", self._on_request), ("response", self._on_response), ("requestfinished", self._on_done), ("requestfailed", self._on_failed)]
        for event, handler in self._listeners:
            page.on(event, handler)

    # ---- the page's requests ------------------------------------------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _api(request) -> bool:
        return "/v1/" in request.url and request.resource_type != "eventsource"

    @property
    def in_flight(self) -> list:
        """The ``/v1`` requests the page has made and not had an answer to (an event stream is not one)."""
        return list(self._inflight)

    def _on_request(self, request) -> None:
        # A page load drops the requests of the document it replaces without reporting them finished, so they are forgotten when the NAVIGATION starts (not at domcontentloaded, which would drop the
        # new document's early requests too). The sweep goes from ``/console/#/w/...`` to ``/console/``, the same document URL, so the URL cannot say it.
        if request.is_navigation_request() and request.frame.parent_frame is None:
            self._inflight.clear()
        if self._api(request):
            self._inflight.add(request)

    def _on_response(self, response) -> None:
        request = response.request
        if request.is_navigation_request() and request.frame.parent_frame is None:
            # The new document has committed. Until now the old one was alive and its requests were never reported finished when it went (as Playwright's own network-idle counts, at commit): what is in
            # flight now can only be the new document's own, and its first request comes after this response.
            self._inflight.clear()
        if is_event_stream(request.resource_type, response.headers.get("content-type")):
            self._inflight.discard(request)
            return
        problem = api_problem(method=request.method, url=request.url, resource_type=request.resource_type, status=response.status)
        if problem:
            self.network.append(problem)

    def _on_done(self, request) -> None:
        self._inflight.discard(request)

    def _on_failed(self, request) -> None:
        self._inflight.discard(request)
        problem = api_problem(method=request.method, url=request.url, resource_type=request.resource_type, failure=request.failure or "failed")
        if problem:
            self.network.append(problem)

    def close(self) -> None:
        """Note what the page's last moments did to the network (they belong to a note too), stop listening and detach the probe. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self.note_network("after the last look")
        for event, handler in self._listeners:
            self.page.remove_listener(event, handler)
        self.probe.close()

    # ---- waiting ------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def arrive(self, kind: str, name: str, surface: str | None = None) -> bool:
        """Wait for the page of ``kind``/``name`` to show every marker of ``ready_selectors``. ``False`` (and a note) when one never shows."""
        label = surface or f"{kind} {name}"
        self.budget.check(label)
        for marker in ready_selectors(kind, name):
            try:
                expect(self.page.locator(marker).first).to_be_visible(timeout=self.budget.wait_ms(self.ready_timeout_ms))
            except AssertionError:
                self.budget.spent()
                self.notes.append(f"{label}: never showed {marker}")
                return False
        return True

    def reach(self, surface: str, open_it: Callable[[], None]) -> bool:
        """Run ``open_it`` (a navigation to ``surface``) and say whether it worked. A navigation that times out (``open_legacy_route`` raises ``AssertionError`` after its own two timeouts) is a note and an
        empty look for the surface, and ``False``: the sweep goes on to the other surfaces. The deadline is not a navigation failure and goes through."""
        try:
            open_it()
        except (AssertionError, BrowserError) as exc:
            self.budget.spent()
            self.skip(surface, f"it could not be opened ({type(exc).__name__}: {str(exc).splitlines()[0][:120] if str(exc) else ''})")
            return False
        return True

    def _wait_until_idle(self, surface: str) -> None:
        """Wait until no ``/v1`` request is in flight: after a round trip to the page (a request the page has just started is announced to us before the answer to that round trip, so the set is
        not read before the client has been told of it), and for a QUIET window: the set empty on two checks a quarter of a second apart, so a request that starts within the quiet window (a poll that
        starts as the look begins, a fetch the page makes 120 ms in) is waited for too. The wait is what the budget allows, and raises past its deadline."""
        deadline = time.monotonic() + self.budget.wait_ms(int(self.idle_timeout_s * 1000)) / 1000
        quiet = 0
        while True:
            self.page.evaluate("1")
            quiet = 0 if self._inflight else quiet + 1
            if quiet >= 2 or time.monotonic() >= deadline:
                break
            self.page.wait_for_timeout(100 if self._inflight else 250)
        if self._inflight:
            self.budget.spent()
            self.notes.append(f"{surface}: still waiting for " + ", ".join(sorted(f"{r.method} {r.url.split('?', 1)[0].split('/v1/', 1)[-1]}" for r in self._inflight)))

    # ---- the look -----------------------------------------------------------------------------------------------------------------------------------------------------------------

    def at(self, surface: str, root: str | None = None, *, ready: list[str] | None = None, check_page: bool = True) -> None:
        """Record the unnamed controls under ``root`` (a CSS selector that must match a visible element; the whole page when none) as the page is now, under ``surface``.

        Before it looks, the surface must show every marker of ``ready``, its ``/v1`` requests must have answered and, with ``check_page``, what is loading must have stopped; an error banner under
        it, a marker that never came, a request that failed or answered 500 or more are notes in the report (the look is still taken, so one broken page does not hide the rest)."""
        self.budget.check(surface)
        self.visited.append(surface)
        for marker in ready or []:
            try:
                expect(self.page.locator(marker).first).to_be_visible(timeout=self.budget.wait_ms(self.ready_timeout_ms))
            except AssertionError:
                self.budget.spent()
                self.notes.append(f"{surface}: never showed {marker}")
        self._wait_until_idle(surface)
        state = page_state(self.page, root)
        if check_page:
            deadline = time.monotonic() + self.budget.wait_ms(int(self.loaded_timeout_s * 1000)) / 1000
            while state.loading and time.monotonic() < deadline:
                self.page.wait_for_timeout(250)
                state = page_state(self.page, root)
            if state.loading:
                self.budget.spent()
                self.notes.append(f"{surface}: still loading after {self.loaded_timeout_s} s: {state.loading}")
            if state.errors:
                self.notes.append(f"{surface}: an error banner under it: {state.errors}")
        self.page.wait_for_timeout(250)  # let a just-opened surface finish painting
        try:
            result = self.probe.examine(root, chrome=CHROME)
        except AssertionError as exc:   # the page kept changing under the probe, even after its retry: this surface is noted and empty, the sweep goes on
            self.notes.append(f"{surface}: could not be looked at: {exc}")
            self.looks[surface] = Look()
            self.note_network(surface)
            return
        self.looks[surface] = Look(examined=result.examined, body=result.body, skipped=result.skipped)
        for item in result.unnamed:
            self.found.setdefault(item["html"], []).append(surface)
        self.note_network(surface)

    def note_network(self, surface: str) -> None:
        """The requests that failed or answered 500 or more since the last look are this surface's notes (each once)."""
        for problem in dict.fromkeys(self.network[self._reported:]):
            self.notes.append(f"{surface}: {problem}")
        self._reported = len(self.network)

    def skip(self, surface: str, why: str) -> None:
        """A surface that could not be reached: visited, with nothing in its body, and the reason noted (the floor then reports it too)."""
        self.visited.append(surface)
        self.looks[surface] = Look()
        self.notes.append(f"{surface}: not looked at, {why}")
        self.note_network(surface)
