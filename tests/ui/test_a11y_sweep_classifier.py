"""The standing a11y sweep's rule, without a browser (console review C-003).

``tests/ui_e2e/test_console_controls_have_names_sweep.py`` fails on any control Chromium gives no accessible name that ``ALLOWLIST`` (``tests/ui_e2e/_a11y.py``) does not excuse, and on any
allowlist entry that excused nothing in the run. Chromium decides what a control is called (the accessibility tree over CDP); two pure functions decide what that answer means and these cases
pin them on the node shapes Chromium returns: ``verdict`` (skipped, named, or unnamed and why) and ``classify`` (what the allowlist excuses), and the shape of the list itself.
"""

from __future__ import annotations

import re

from tests.ui_e2e._a11y import ALLOWLIST, Look, classify, counts_table, effective_source, evaluate_sweep, verdict

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
    """Chromium returns the blanks it was given: spaces, non-breaking spaces, a zero-width space, a byte-order mark and a word joiner are no name (written as escapes, so that the file shows them)."""
    for blank in ("   ", "\u00a0\u00a0", "\u200b", " \u200b\u200c\u200d ", "\ufeff", "\u2060"):
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


def test_a_control_named_only_by_an_aria_labelledby_that_points_at_itself_is_named_by_its_placeholder() -> None:
    """N11: ``aria-labelledby`` naming the input itself makes Chromium read its placeholder as the name, from a source that looks like a label."""
    self_ref = [{"type": "relatedElement", "attribute": "aria-labelledby", "value": {"value": "Filter"},
                 "attributeValue": {"relatedNodes": [{"backendDOMNodeId": 9, "idref": "f", "text": "Filter"}]}},
                {"type": "placeholder", "attribute": "placeholder", "superseded": True, "value": {"value": "Filter"}}]
    node = {**_ax("Filter", self_ref), "backendDOMNodeId": 9}
    assert verdict(node) == ("unnamed", "its only name is its placeholder (its aria-labelledby points at itself)")


def test_an_aria_labelledby_that_reaches_another_element_is_a_name_even_when_it_also_reaches_the_control() -> None:
    other = [{"type": "relatedElement", "attribute": "aria-labelledby", "value": {"value": "Filter things"},
              "attributeValue": {"relatedNodes": [{"backendDOMNodeId": 9, "idref": "f", "text": "Filter"}, {"backendDOMNodeId": 12, "idref": "h", "text": "things"}]}},
             {"type": "placeholder", "attribute": "placeholder", "superseded": True, "value": {"value": "Filter"}}]
    assert verdict({**_ax("Filter things", other), "backendDOMNodeId": 9}) == ("named", "relatedElement")
    empty_other = [{"type": "relatedElement", "attribute": "aria-labelledby", "value": {"value": "Filter"},
                    "attributeValue": {"relatedNodes": [{"backendDOMNodeId": 9, "idref": "f", "text": "Filter"}, {"backendDOMNodeId": 12, "idref": "h", "text": ""}]}},
                   {"type": "placeholder", "attribute": "placeholder", "superseded": True, "value": {"value": "Filter"}}]
    assert verdict({**_ax("Filter", empty_other), "backendDOMNodeId": 9})[0] == "unnamed", "an empty element beside itself names nothing"


# ---------------------------------------------------------------------------
# evaluate_sweep: every guard of the end of the sweep, without a browser (review of #668, N3)
# ---------------------------------------------------------------------------

SURFACES = ["studio", "page one", "page one / New thing"]
FLOORS = {"studio": 5, "page one": 3, "page one / New thing": 2}
LOOKS = {"studio": Look(examined=40, body=30, skipped=0), "page one": Look(examined=9, body=5, skipped=1), "page one / New thing": Look(examined=6, body=4, skipped=0)}


def _evaluate(**changes) -> list[str]:
    arguments = {"found": {}, "allowlist": [], "visited": list(SURFACES), "expected": list(SURFACES), "looks": dict(LOOKS), "floors": dict(FLOORS), "notes": [], "page_errors": [], "left": []}
    arguments.update(changes)
    return evaluate_sweep(**arguments)


def test_a_clean_complete_sweep_has_no_problem() -> None:
    assert _evaluate() == []


def test_an_unnamed_control_is_a_problem_with_its_markup_and_its_surfaces() -> None:
    problems = _evaluate(found={A: ["page one", "studio"]})
    assert len(problems) == 1 and "1 control(s) with no name" in problems[0] and "filter-a" in problems[0] and "page one, studio" in problems[0]


def test_a_visit_that_did_not_happen_or_happened_twice_is_a_problem() -> None:
    missing = _evaluate(visited=["studio", "page one"])
    assert len(missing) == 1 and "did not visit exactly" in missing[0] and "page one / New thing" in missing[0]
    twice = _evaluate(visited=SURFACES + ["studio"])
    assert len(twice) == 1 and "did not visit exactly" in twice[0]


def test_a_surface_whose_body_is_under_its_floor_is_a_problem_even_when_its_chrome_is_not() -> None:
    """The body, not everything examined: 'page one' examined 9 controls, 5 of them in the body, against a floor of 6."""
    problems = _evaluate(floors={**FLOORS, "page one": 6})
    assert len(problems) == 1 and "page one: 5 control(s) in its body, at least 6 expected" in problems[0]


def test_a_surface_with_no_floor_is_a_problem() -> None:
    problems = _evaluate(floors={"studio": 5, "page one": 3})
    assert len(problems) == 1 and "page one / New thing" in problems[0] and "no floor" in problems[0]


def test_a_look_that_never_happened_is_not_a_floor_failure_but_the_visit_list_says_so() -> None:
    looks = {k: v for k, v in LOOKS.items() if k != "page one / New thing"}
    problems = _evaluate(looks=looks, visited=["studio", "page one"])
    assert len(problems) == 1 and "did not visit exactly" in problems[0]


def test_what_a_page_said_about_itself_is_a_problem() -> None:
    problems = _evaluate(notes=["page one: an error banner under the page: 'Could not load'"])
    assert problems == ["problems with the pages themselves:\n  page one: an error banner under the page: 'Could not load'"]


def test_a_page_error_is_a_problem() -> None:
    assert _evaluate(page_errors=["TypeError: x is undefined"]) == ["page errors during the sweep: ['TypeError: x is undefined']"]


def test_an_allowlist_entry_that_excused_nothing_is_a_problem() -> None:
    problems = _evaluate(allowlist=[('data-testid="gone"', "reason with enough words")])
    assert len(problems) == 1 and "allowlist entries that match nothing" in problems[0] and 'data-testid="gone"' in problems[0]


def test_seeded_rows_that_could_not_be_deleted_are_a_problem() -> None:
    problems = _evaluate(left=["/v1/graphs/g (500)"])
    assert problems == ["seeded rows that could not be deleted: ['/v1/graphs/g (500)']"]


def test_a_sweep_that_died_does_not_also_complain_about_the_visits_it_never_made() -> None:
    assert _evaluate(visited=["studio"], completed=False) == []
    assert len(_evaluate(visited=["studio"], completed=False, page_errors=["boom"])) == 1


def test_every_kind_of_failure_is_reported_together() -> None:
    problems = _evaluate(found={A: ["studio"]}, visited=["studio"], floors={**FLOORS, "studio": 99}, page_errors=["boom"], left=["/x"], notes=["n"], allowlist=[("zz", "reason with enough words")])
    assert len(problems) == 7, problems


def test_the_counts_table_shows_what_each_surface_examined_in_its_body_and_its_floor() -> None:
    table = counts_table(LOOKS, FLOORS)
    lines = table.splitlines()
    assert lines[0].split() == ["surface", "examined", "body", "floor", "skipped"]
    assert any(line.split()[-4:] == ["9", "5", "3", "1"] and line.startswith("page one ") for line in lines)
    assert any(line.startswith("page one / New thing") and line.split()[-4:] == ["6", "4", "2", "0"] for line in lines)
