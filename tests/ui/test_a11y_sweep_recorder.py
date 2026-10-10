"""``Sweep``, the recorder of the standing a11y sweep, on a page that is only a script (review of #668, round 7 nits; the lead's mutant survivors).

``tests/ui_e2e/test_a11y_sweep_guards_known_answers.py`` drives ``Sweep`` on a real page and kills every one of these in a browser. ``tests/ui`` never starts one, so a change to the recorder's waiting and bounding
was invisible to the lane that runs on every commit. These cases give ``Sweep`` a page that does only what it asks of one (``on``, ``evaluate``, ``set_default_timeout`` ...), with the requests and the clock a script
decides, so the logic is pinned without a browser: the idle wait needs a QUIET window and keeps every request in flight, a page that cannot be opened spends the budget, the page's defaults are what the budget
has left before ``arrive`` and ``at`` wait, and ``close`` gives the page Playwright's own default back.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from collections.abc import Callable

import pytest

from tests.ui_e2e import _a11y_sweep as sweep_module
from tests.ui_e2e._a11y import CANDIDATES_JS, PAGE_STATE_JS, Budget, SweepDeadlineExceeded
from tests.ui_e2e._a11y_sweep import Sweep


class _Request:
    """A ``/v1`` request as ``Sweep``'s listeners read it."""

    def __init__(self, path: str) -> None:
        self.url = f"http://console.test/v1/{path}"
        self.method = "GET"
        self.resource_type = "fetch"
        self.failure = None
        self.frame = SimpleNamespace(parent_frame=None)
        self.finished = False

    def is_navigation_request(self) -> bool:
        return False


class _Cdp:
    def send(self, *args: Any, **kwargs: Any) -> dict:
        return {}

    def detach(self) -> None:
        pass


class _Page:
    """What ``Sweep`` asks of a page. ``round_trip(page, n)`` runs inside the n-th ``evaluate("1")`` (the round trip the idle wait makes), which is where a script starts or finishes a request."""

    def __init__(self, round_trip: Callable[["_Page", int], None] | None = None) -> None:
        self.handlers: dict[str, Callable] = {}
        self.round_trips = 0
        self.round_trip = round_trip
        self.events: list[tuple[str, int]] = []
        self.context = SimpleNamespace(new_cdp_session=lambda page: _Cdp())

    def on(self, event: str, handler: Callable) -> None:
        self.handlers[event] = handler

    def remove_listener(self, event: str, handler: Callable) -> None:
        self.handlers.pop(event, None)

    def emit(self, event: str, request: _Request) -> None:
        if event == "requestfinished":
            request.finished = True
        self.handlers[event](request)

    def set_default_timeout(self, ms: int) -> None:
        self.events.append(("default", ms))

    def set_default_navigation_timeout(self, ms: int) -> None:
        self.events.append(("navigation", ms))

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def locator(self, selector: str) -> Any:
        return SimpleNamespace(first=selector)

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if script == "1":
            self.round_trips += 1
            if self.round_trip:
                self.round_trip(self, self.round_trips)
            return 1
        if script is PAGE_STATE_JS:
            return {"error": None, "loading": [], "errors": []}
        if script is CANDIDATES_JS:
            return {"error": None, "candidates": []}
        return None           # the probe's clean-up

    def defaults(self, kind: str = "default") -> list[int]:
        return [ms for name, ms in self.events if name == kind]


@pytest.fixture
def waited(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """``expect(locator).to_be_visible(timeout=...)`` is Playwright's; it is replaced by a recorder of the timeouts the sweep asks for, in order with the defaults it set."""
    seen: list[tuple[str, int]] = []

    def fake_expect(locator: Any) -> Any:
        return SimpleNamespace(to_be_visible=lambda timeout: seen.append((locator, timeout)))

    monkeypatch.setattr(sweep_module, "expect", fake_expect)
    return seen


def _sweep(page: _Page, **kw: Any) -> Sweep:
    return Sweep(page, idle_timeout_s=5, **kw)       # type: ignore[arg-type]


# ---- the idle wait ------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_look_waits_for_a_request_that_starts_after_the_first_empty_check() -> None:
    """The set is empty on the first check; the page starts a fetch before the second (a poll that begins as the look does, a fetch 120 ms in). A wait that stops at the first empty check looks at the page
    with that request still out; the quiet window (two empty checks in a row) waits for it."""
    late = _Request("late")

    def script(page: _Page, n: int) -> None:
        if n == 2:
            page.emit("request", late)
        if n == 4:
            page.emit("requestfinished", late)

    page = _Page(script)
    sweep = _sweep(page)
    sweep.at("surface")
    assert late.finished and sweep.in_flight == [], "the look was taken with a request out"
    assert page.round_trips >= 5, page.round_trips


def test_a_request_that_finishes_does_not_make_the_look_forget_the_ones_still_out() -> None:
    """Two requests overlap: the first is answered while the second is still out. The finished one removes ITSELF from the set; a wait that cleared the set would look at the page with the second out."""
    first, second = _Request("first"), _Request("second")

    def script(page: _Page, n: int) -> None:
        if n == 1:
            page.emit("request", first)
            page.emit("request", second)
        if n == 2:
            page.emit("requestfinished", first)
        if n == 5:
            page.emit("requestfinished", second)

    page = _Page(script)
    sweep = _sweep(page)
    sweep.at("surface")
    assert second.finished and sweep.in_flight == [], "the look was taken before the second request was answered"
    assert page.round_trips >= 6, page.round_trips


# ---- the budget bounds the page's own defaults --------------------------------------------------------------------------------------------------------------------------------


def test_the_pages_defaults_are_what_the_budget_has_left() -> None:
    """A call with no timeout of its own (``goto``, ``get_attribute``: 30 s in Playwright) takes the page's default, which is what the budget allows: the deadline's remainder when it is less than 30 s, the
    short wait once enough looks used theirs up, and past the deadline no default at all (it raises)."""
    clock = [1000.0]
    page = _Page()
    sweep = _sweep(page, budget=Budget(limit=1, short_ms=1_000, deadline_s=100, clock=lambda: clock[0]))
    sweep.bound_defaults()
    assert page.defaults() == [30_000] and page.defaults("navigation") == [30_000]
    clock[0] = 1085.0                 # 15 s left
    sweep.bound_defaults()
    assert page.defaults()[-1] == 15_000 and page.defaults("navigation")[-1] == 15_000
    sweep.budget.spent()              # the limit is 1: later waits are short
    sweep.bound_defaults()
    assert page.defaults()[-1] == 1_000 and page.defaults("navigation")[-1] == 1_000
    clock[0] = 1101.0
    with pytest.raises(SweepDeadlineExceeded):
        sweep.bound_defaults()


def test_at_bounds_the_pages_defaults_before_it_waits() -> None:
    clock = [1000.0]
    page = _Page()
    sweep = _sweep(page, budget=Budget(deadline_s=100, clock=lambda: clock[0]))
    sweep.budget.check("start")
    clock[0] = 1085.0
    sweep.at("surface")
    assert page.defaults() == [15_000] and page.defaults("navigation") == [15_000], page.events


def test_arrive_bounds_the_pages_defaults_before_it_waits_and_waits_what_the_budget_allows(waited: list[tuple[str, int]]) -> None:
    clock = [1000.0]
    page = _Page()
    sweep = _sweep(page, budget=Budget(deadline_s=100, clock=lambda: clock[0]))
    sweep.budget.check("start")
    clock[0] = 1085.0
    assert sweep.arrive("system-view", "settings") is True
    assert page.defaults() == [15_000] and page.defaults("navigation") == [15_000], page.events
    assert waited and all(timeout == 15_000 for _marker, timeout in waited), waited


# ---- a page that cannot be opened ------------------------------------------------------------------------------------------------------------------------------------------


def test_a_page_that_cannot_be_opened_spends_the_budget_and_is_a_note() -> None:
    page = _Page()
    sweep = _sweep(page, budget=Budget(limit=2))

    def stuck() -> None:
        raise AssertionError("never came up")

    assert sweep.budget.used == 0
    assert sweep.reach("one", stuck) is False
    assert sweep.budget.used == 1, "a navigation that timed out is a stuck look like any other"
    assert sweep.reach("two", stuck) is False
    assert sweep.budget.exhausted and sweep.visited == ["one", "two"]
    assert any(note.startswith("one: not looked at, it could not be opened") for note in sweep.notes), sweep.notes


# ---- close -----------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_close_gives_the_page_playwrights_default_back() -> None:
    """The sweep leaves the page's default at what the budget had left, which near the deadline is a few hundred milliseconds: whatever the test or the fixture's teardown does with the page after
    ``close`` must not inherit it."""
    clock = [1000.0]
    page = _Page()
    sweep = _sweep(page, budget=Budget(deadline_s=100, clock=lambda: clock[0]))
    sweep.budget.check("start")       # the clock starts with the first wait or check
    clock[0] = 1099.5
    sweep.bound_defaults()
    assert page.defaults()[-1] == 500
    sweep.close()
    assert page.defaults()[-1] == 30_000 and page.defaults("navigation")[-1] == 30_000
    assert page.handlers == {}, "and it stopped listening"
    sweep.close()                     # safe to call twice
