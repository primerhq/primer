"""The standing a11y sweep's rule, without a browser (console review C-003).

``tests/ui_e2e/test_console_controls_have_names_sweep.py`` fails on any control Chromium gives no accessible name that ``ALLOWLIST`` (``tests/ui_e2e/_a11y.py``) does not excuse, and on any
allowlist entry that excused nothing in the run. Chromium decides what a control is called (the accessibility tree over CDP); two pure functions decide what that answer means and these cases
pin them on the node shapes Chromium returns: ``verdict`` (skipped, named, or unnamed and why) and ``classify`` (what the allowlist excuses), and the shape of the list itself.
"""

from __future__ import annotations

import re

from tests.ui_e2e._a11y import ALLOWLIST, classify, effective_source, verdict

A = '<input class="input" placeholder="Filter a" data-testid="filter-a">'
B = '<select class="select" data-testid="kind-b"><option value="">all</option></select>'


def test_without_an_allowlist_every_control_found_is_unnamed_and_nothing_is_stale() -> None:
    unnamed, stale = classify({A: ["page 2", "page 1", "page 2"], B: ["page 3"]}, [])
    assert unnamed == {A: ["page 1", "page 2"], B: ["page 3"]}, "surfaces are de-duplicated and sorted (the input is neither)"
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


def test_every_entry_that_matches_a_control_is_credited_not_only_the_first() -> None:
    """The first entry is broad and shadows the second; the second still excuses the control, so neither is stale because of the order of the list."""
    unnamed, stale = classify({A: ["p"]}, [(r"<input", "reason one"), ('data-testid="filter-a"', "reason two")])
    assert unnamed == {} and stale == []


def test_a_pattern_is_a_regex_searched_in_the_outer_html_not_a_full_match() -> None:
    assert classify({A: ["p"]}, [(r"placeholder=\"Filter \w+\"", "reason")]) == ({}, [])
    assert classify({A: ["p"]}, [(r"^placeholder", "reason")]) == ({A: ["p"]}, [r"^placeholder"])


def test_nothing_is_found_when_the_page_is_clean_and_every_entry_is_then_stale() -> None:
    assert classify({}, [("x", "reason")]) == ({}, ["x"])


def test_the_allowlist_is_empty() -> None:
    """Pinned empty, not capped: the rule is "it may only shrink", and a cap of 3 let it grow from 0 to 3 unnoticed. A control that cannot be named is a design question for a ticket, not an
    entry here; if one ever has to be, this assertion is the place that says so in the diff (with its reason, which ``test_an_entry_says_why`` holds to a sentence)."""
    assert ALLOWLIST == []


def test_an_entry_says_why() -> None:
    for pattern, reason in [('data-testid="x"', "ticket 01a1: a third-party widget cannot be named")]:
        re.compile(pattern)
        assert len(reason.split()) >= 4
    for pattern, reason in ALLOWLIST:
        re.compile(pattern)
        assert len(reason.split()) >= 4, f"{pattern!r}: an entry needs a reason (a ticket, or why it cannot be named)"


# ---------------------------------------------------------------------------
# verdict: what Chromium's accessibility tree says, and what it means
# ---------------------------------------------------------------------------


def _ax(name: str | None, sources: list[dict] | None = None, *, ignored: bool = False, role: str = "textbox") -> dict:
    node: dict = {"ignored": ignored, "role": {"value": role}}
    if name is not None:
        node["name"] = {"value": name, "sources": sources or []}
    return node


PLACEHOLDER = [{"type": "attribute", "attribute": "title"}, {"type": "placeholder", "attribute": "placeholder", "value": {"type": "string", "value": "Filter"}}]
LABEL = [{"type": "relatedElement", "nativeSource": "labelfor", "value": {"type": "string", "value": "Filter"}}, {"type": "placeholder", "attribute": "placeholder", "superseded": True,
                                                                                                                  "value": {"type": "string", "value": "e.g."}}]


def test_a_control_outside_the_accessibility_tree_is_skipped_not_flagged() -> None:
    assert verdict(None)[0] == "skipped"
    assert verdict(_ax(None, ignored=True))[0] == "skipped"
    assert verdict({"ignored": True, "role": {"value": "none"}})[0] == "skipped"


def test_an_empty_name_is_unnamed() -> None:
    assert verdict(_ax("")) == ("unnamed", "no accessible name")
    assert verdict({"ignored": False, "role": {"value": "button"}}) == ("unnamed", "no accessible name"), "a node with no name at all"


def test_a_name_of_only_blanks_is_unnamed() -> None:
    """Chromium returns the blanks it was given: spaces, non-breaking spaces and a zero-width space are no name."""
    for blank in ("   ", "  ", "​", " ​  ", "﻿", "⁠"):
        assert verdict(_ax(blank))[0] == "unnamed", repr(blank)


def test_a_name_that_is_only_the_placeholder_is_unnamed() -> None:
    assert verdict(_ax("Filter", PLACEHOLDER)) == ("unnamed", "its only name is its placeholder")


def test_a_label_beside_a_placeholder_is_a_name_and_so_is_a_title() -> None:
    assert verdict(_ax("Filter", LABEL)) == ("named", "labelfor")
    titled = [{"type": "attribute", "attribute": "title", "value": {"type": "string", "value": "Search"}}, {"type": "placeholder", "attribute": "placeholder", "superseded": True,
                                                                                                           "value": {"type": "string", "value": "Filter"}}]
    assert verdict(_ax("Search", titled)) == ("named", "attribute")


def test_the_source_is_the_first_one_that_produced_a_value_and_was_not_superseded() -> None:
    sources = [
        {"type": "relatedElement", "attribute": "aria-labelledby"},                                                         # no value
        {"type": "attribute", "attribute": "aria-label", "value": {"value": "Real"}, "superseded": True},                    # superseded
        {"type": "attribute", "attribute": "title", "value": {"value": "Tt"}, "invalid": True},                              # invalid
        {"type": "contents", "value": {"value": "Save"}},                                                                   # the one
        {"type": "placeholder", "value": {"value": "P"}},
    ]
    assert effective_source(sources) == "contents"
    assert effective_source([]) == "" and effective_source([{"type": "contents"}]) == ""


def test_a_name_with_no_source_information_is_named() -> None:
    """The conservative reading: Chromium gave a name and did not say where it came from."""
    assert verdict(_ax("Save", [])) == ("named", "name")
