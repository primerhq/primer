"""The standing sweep's allowlist can only shrink (console review C-003).

``tests/ui_e2e/test_console_controls_have_names_sweep.py`` fails on any visible control with no accessible name that ``ALLOWLIST`` (``tests/ui_e2e/_a11y.py``) does not excuse, and
on any allowlist entry that excused nothing in the run. ``classify`` is the pure half of that rule; these cases pin it without a browser, and pin the shape of the list itself.
"""

from __future__ import annotations

import re

from tests.ui_e2e._a11y import ALLOWLIST, classify

A = '<input class="input" placeholder="Filter a" data-testid="filter-a">'
B = '<select class="select" data-testid="kind-b"><option value="">all</option></select>'


def test_without_an_allowlist_every_control_found_is_unnamed_and_nothing_is_stale() -> None:
    unnamed, stale = classify({A: ["page 1", "page 2", "page 1"], B: ["page 3"]}, [])
    assert unnamed == {A: ["page 1", "page 2"], B: ["page 3"]}, "surfaces are de-duplicated and sorted"
    assert stale == []


def test_an_entry_excuses_the_control_it_matches_and_only_that_one() -> None:
    unnamed, stale = classify({A: ["p"], B: ["p"]}, [('data-testid="filter-a"', "ticket 01a1: the filter is a third-party widget")])
    assert list(unnamed) == [B]
    assert stale == []


def test_an_entry_that_matches_nothing_is_stale() -> None:
    unnamed, stale = classify({A: ["p"]}, [('data-testid="filter-a"', "reason one"), ('data-testid="gone"', "reason two")])
    assert unnamed == {}
    assert stale == ['data-testid="gone"'], "the control it excused was fixed or removed: the entry has to go"


def test_one_entry_may_excuse_several_controls_and_is_then_not_stale() -> None:
    unnamed, stale = classify({A: ["p"], B: ["p"]}, [(r"data-testid=", "reason")])
    assert unnamed == {} and stale == []


def test_a_pattern_is_a_regex_searched_in_the_outer_html_not_a_full_match() -> None:
    assert classify({A: ["p"]}, [(r"placeholder=\"Filter \w+\"", "reason")]) == ({}, [])
    assert classify({A: ["p"]}, [(r"^placeholder", "reason")]) == ({A: ["p"]}, [r"^placeholder"])


def test_nothing_is_found_when_the_page_is_clean_and_every_entry_is_then_stale() -> None:
    assert classify({}, [("x", "reason")]) == ({}, ["x"])


def test_the_allowlist_is_small_and_every_entry_says_why() -> None:
    assert len(ALLOWLIST) <= 3, "the allowlist is for a control that CANNOT be named; name the control instead of adding to it"
    for pattern, reason in ALLOWLIST:
        re.compile(pattern)
        assert len(reason.split()) >= 4, f"{pattern!r}: an entry needs a reason (a ticket, or why it cannot be named)"
