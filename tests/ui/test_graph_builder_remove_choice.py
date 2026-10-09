"""The builder can remove a choice between paths, one condition of a branch, and a branch (board tickets 01a11e4e-6fa7 and 01a11e4e-7059, found in the lead's review of #633).

The retired legacy editor deleted ANY selected edge and had an x per condition inside a branch. In the builder ``DELETE_EDGE`` was reachable only from the static-edge branch of the edge
inspector, so a choice between paths (a conditional edge) could not be removed except by deleting its source or target step, and ``GB_BranchBuilder`` could add a condition and a branch but
not remove one condition (the branch's own x was an unlabelled span). Now:

* a conditional edge has a named **Remove this choice** (in the edge's inspector and under the step's "What happens next"), asking first, through the console's own confirmDialog (focus trap, Escape, focus returned), when it carries paths;
* every condition has a named remove, and a branch with a single condition becomes "Always" when it is removed;
* a branch has a named remove that is OFF for the only branch of a choice (the model's ``_JsonPathRouter.branches`` needs at least one; #633's validator rule stays blocking for an empty list);
* the static-edge control is unchanged.

The REAL builder runs in V8 on the strict mini React (``tests/ui/_graph_builder_v8.py`` has the harness).
"""

from __future__ import annotations

import copy
import json

import pytest

from tests.ui._mini_react import mini_react_context
from tests.ui._graph_builder_v8 import DRIVER, PRELUDE, builder_code
from tests.ui.test_graph_builder_import_shapes import BASE


def _two_paths() -> dict:
    """BASE with the conditional edge of step ``a`` holding two paths, the first with two conditions."""
    spec = copy.deepcopy(BASE)
    router = spec["edges"][1]["router"]
    router["branches"] = [
        {"conditions": [{"path": "ok", "op": "eq", "value": True}, {"path": "n", "op": "gt", "value": 3}], "to_node": "f"},
        {"conditions": [{"path": "ok", "op": "eq", "value": False}], "to_node": "t"},
    ]
    return spec


# the console's confirmDialog, as a promise the test answers: every question asked is kept with its options
CONFIRM = """
var __confirms = [];
var confirmDialog = function (opts) { return new Promise(function (resolve) { __confirms.push({ opts: opts, resolve: resolve }); }); };
"""

# Ctrl/Cmd-Z, to check an undone remove
KEYS = """
var __KEYS = [];
window.addEventListener = function (t, f) { if (t === 'keydown') __KEYS.push(f); };
window.removeEventListener = function (t, f) { var i = __KEYS.indexOf(f); if (i >= 0) __KEYS.splice(i, 1); };
function pressUndo() { __KEYS.slice().forEach(function (f) { f({ key: 'z', metaKey: true, ctrlKey: false, shiftKey: false, preventDefault: function () {} }); }); MR.rerender(); }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(builder_code(), PRELUDE + CONFIRM + KEYS)
    c.eval(DRIVER)
    c.eval("function draftOf() { return inspector().props.draft; }")
    c.eval("function boxes() { return MR.findAll('gb-branch-value').filter(function (el) { return el.type === 'input'; }); }")
    c.eval("function typeInto(i, text) { boxes()[i].props.onChange({ target: { value: text } }); MR.rerender(); }")
    c.eval("function canvas() { return findType(MR.find('gb-builder'), GB_Canvas); }")
    try:
        yield c
    finally:
        c.close()


def _mount(ctx, spec: dict, *, select: str = "a", read_only: bool = False) -> None:
    spec = {**spec, **({"harness_id": "managed"} if read_only else {})}
    ctx.eval(f"mountBuilder({json.dumps(spec)}); selectNode({json.dumps(select)});")


def _edges(ctx) -> list[dict]:
    return json.loads(ctx.eval("JSON.stringify(draftOf().edges)"))


def _branches(ctx) -> list[dict]:
    return next(e for e in _edges(ctx) if e["kind"] == "conditional")["router"]["branches"]


def _find(ctx, testid: str):
    return ctx.eval(f"MR.find({json.dumps(testid)}) !== null")


def _answer(ctx, ok: bool) -> dict:
    """Answer the oldest question the builder put to confirmDialog, let the promise jobs run, redraw; returns the options it was asked with."""
    opts = json.loads(ctx.eval("JSON.stringify(__confirms[0].opts)"))
    ctx.eval(f"__confirms.shift().resolve({'true' if ok else 'false'});")
    ctx.eval("void 0;")
    ctx.eval("MR.rerender();")
    return opts


def _asked(ctx) -> int:
    return ctx.eval("__confirms.length")


def _click(ctx, testid: str) -> None:
    ctx.eval(f"MR.click({json.dumps(testid)}); MR.rerender();")


def _by_label(ctx, label: str):
    """The element (any type) whose aria-label is ``label`` in the current tree, as {found, disabled}."""
    return json.loads(ctx.eval(
        "(function () { var out = null; (function w(n) { if (out || n == null || typeof n !== 'object') return; if (Array.isArray(n)) { n.forEach(w); return; } if (!n.__el) return;"
        f" if (n.props && n.props['aria-label'] === {json.dumps(label)}) out = {{ found: true, disabled: !!n.props.disabled, ariaDisabled: n.props['aria-disabled'] || null, type: String(n.type) }};"
        " else if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children); })(MR.find('gb-builder')); return JSON.stringify(out); })()"
    ))


def _press(ctx, label: str) -> None:
    ctx.eval(
        "(function () { var hit = null; (function w(n) { if (hit || n == null || typeof n !== 'object') return; if (Array.isArray(n)) { n.forEach(w); return; } if (!n.__el) return;"
        f" if (n.props && n.props['aria-label'] === {json.dumps(label)}) hit = n;"
        " else if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children); })(MR.find('gb-builder'));"
        " hit.props.onClick({ preventDefault: function () {}, stopPropagation: function () {} }); MR.rerender(); })()"
    )


# ---------------------------------------------------------------------------
# a condition
# ---------------------------------------------------------------------------


def test_each_condition_has_a_named_remove_and_it_removes_only_that_condition(ctx) -> None:
    _mount(ctx, _two_paths())
    for label in ("Branch 1, condition 1: remove", "Branch 1, condition 2: remove", "Branch 2, condition 1: remove"):
        found = _by_label(ctx, label)
        assert found and found["found"] and found["type"] == "button", (label, found)
    _press(ctx, "Branch 1, condition 2: remove")
    branches = _branches(ctx)
    assert [len(b["conditions"]) for b in branches] == [1, 1]
    assert branches[0]["conditions"][0]["path"] == "ok" and branches[0]["to_node"] == "f", "the other condition and the path's target are untouched"


def test_removing_a_condition_does_not_hand_its_half_typed_text_to_the_condition_that_slides_into_its_place(ctx) -> None:
    """The value boxes are keyed by position, and a removed condition's successors slide up one index (the same hazard #679 closed for a deleted path): with the same canonical text the next
    box would inherit the half-typed text, the alert and the Save gate. The key carries the branch's condition count, so removing one remounts that branch's boxes."""
    spec = copy.deepcopy(BASE)
    spec["edges"][1]["router"]["branches"] = [{"conditions": [{"path": "ok", "op": "eq", "value": {"a": 1}}, {"path": "n", "op": "eq", "value": {"a": 1}}], "to_node": "f"}]
    _mount(ctx, spec)
    ctx.eval("typeInto(0, '{\"a\":1}x');")
    assert _find(ctx, "gb-branch-value-error")
    _press(ctx, "Branch 1, condition 1: remove")
    assert json.loads(ctx.eval("JSON.stringify(boxes().map(function (b) { return b.props.value; }))")) == ['{"a":1}']
    assert not _find(ctx, "gb-branch-value-error"), "nothing is half-typed on the condition that slid up"
    remaining = _branches(ctx)[0]["conditions"]
    assert len(remaining) == 1 and remaining[0]["path"] == "n" and remaining[0]["value"] == {"a": 1}


def test_removing_the_last_condition_of_a_branch_leaves_an_always_path_and_the_choice_stays(ctx) -> None:
    _mount(ctx, _two_paths())
    _press(ctx, "Branch 2, condition 1: remove")
    branches = _branches(ctx)
    assert len(branches) == 2 and branches[1]["conditions"] == [] and branches[1]["to_node"] == "t"
    assert "Always (no condition)" in json.loads(ctx.eval("JSON.stringify(MR.texts())"))


def test_a_read_only_builder_has_no_remove_controls(ctx) -> None:
    _mount(ctx, _two_paths(), read_only=True)
    assert _by_label(ctx, "Branch 1, condition 1: remove") is None
    assert _by_label(ctx, "Branch 1: remove") is None
    assert not _find(ctx, "gb-remove-choice")


# ---------------------------------------------------------------------------
# a branch
# ---------------------------------------------------------------------------


def test_a_branch_has_a_named_remove_button_that_removes_that_path(ctx) -> None:
    _mount(ctx, _two_paths())
    found = _by_label(ctx, "Branch 2: remove")
    assert found and found["type"] == "button" and found["disabled"] is False and found["ariaDisabled"] is None, found
    _press(ctx, "Branch 2: remove")
    assert [b["to_node"] for b in _branches(ctx)] == ["f"]


def test_the_only_branch_of_a_choice_cannot_be_removed_because_a_choice_needs_a_path(ctx) -> None:
    _mount(ctx, BASE)   # one path
    found = _by_label(ctx, "Branch 1: remove")
    assert found and found["ariaDisabled"] == "true", found
    _press(ctx, "Branch 1: remove")
    assert len(_branches(ctx)) == 1


def test_the_remove_that_is_off_stays_focusable_and_says_why_in_its_description(ctx) -> None:
    """A ``disabled`` button leaves the tab order and is announced as nothing; ``aria-disabled`` keeps it where the user can reach it, and the reason is its description (N2 of the review)."""
    _mount(ctx, BASE)
    info = json.loads(ctx.eval(
        "(function () { var hit = null; (function w(n) { if (hit || n == null || typeof n !== 'object') return; if (Array.isArray(n)) { n.forEach(w); return; } if (!n.__el) return;"
        " if (n.props && n.props['aria-label'] === 'Branch 1: remove') hit = n; else if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children); })(MR.find('gb-builder'));"
        " var id = hit.props['aria-describedby']; var text = null;"
        " (function w(n) { if (text !== null || n == null || typeof n !== 'object') return; if (Array.isArray(n)) { n.forEach(w); return; } if (!n.__el) return;"
        " if (n.props && n.props.id === id) text = JSON.stringify(n.children); else if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children); })(MR.find('gb-builder'));"
        " return JSON.stringify({ disabled: !!hit.props.disabled, describedby: id || null, reason: text }); })()"
    ))
    assert info["disabled"] is False, "still focusable"
    assert info["describedby"] and info["reason"] and "at least one path" in info["reason"], info


def test_each_path_has_a_named_add_a_condition_button(ctx) -> None:
    _mount(ctx, _two_paths())
    for label in ("Branch 1: add a condition", "Branch 2: add a condition"):
        found = _by_label(ctx, label)
        assert found and found["type"] == "button", (label, found)
    _press(ctx, "Branch 2: add a condition")
    assert [len(b["conditions"]) for b in _branches(ctx)] == [2, 2]


def test_an_empty_list_of_paths_is_still_a_blocking_problem(ctx) -> None:
    """The control: #633's rule (the PUT refuses an empty branch list) is untouched, so a draft that reaches it by any other route is still blocked."""
    empty = copy.deepcopy(BASE)
    empty["edges"][1]["router"]["branches"] = []
    blocking = json.loads(ctx.eval(f"JSON.stringify(GB_validate({json.dumps(empty)}, {{}}).blocking.map(function (r) {{ return r.code; }}))"))
    assert "branches_none" in blocking


# ---------------------------------------------------------------------------
# a choice
# ---------------------------------------------------------------------------


def test_a_choice_can_be_removed_from_the_step_that_makes_it_after_a_confirmation(ctx) -> None:
    _mount(ctx, _two_paths())
    assert _find(ctx, "gb-remove-choice")
    n_edges = len(_edges(ctx))
    _click(ctx, "gb-remove-choice")
    assert len(_edges(ctx)) == n_edges and _asked(ctx) == 1, "the first press asks"
    opts = _answer(ctx, True)
    assert opts["danger"] is True and opts["confirmLabel"] == "Remove" and opts["cancelLabel"] == "Keep", opts
    assert opts["message"] == "Remove this choice, its 2 paths and its 'in any other case' link?", "it says what goes with the choice, the catch-all included"
    edges = _edges(ctx)
    assert len(edges) == n_edges - 1 and not any(e["kind"] == "conditional" and e["from_node"] == "a" for e in edges)


def test_keeping_the_choice_leaves_it_alone(ctx) -> None:
    _mount(ctx, _two_paths())
    _click(ctx, "gb-remove-choice")
    _answer(ctx, False)
    assert any(e["kind"] == "conditional" for e in _edges(ctx))
    assert _find(ctx, "gb-remove-choice")


def test_a_choice_with_one_path_says_path_not_paths(ctx) -> None:
    _mount(ctx, BASE)
    _click(ctx, "gb-remove-choice")
    assert _answer(ctx, False)["message"] == "Remove this choice, its 1 path and its 'in any other case' link?"


def test_a_choice_can_be_removed_from_its_own_inspector_and_the_source_step_is_selected_after(ctx) -> None:
    _mount(ctx, _two_paths(), select="a")
    ctx.eval("selectNode('a'); canvas().props.onEdgeClick(1); MR.rerender();")
    assert ctx.eval("inspector().props.edgeIdx") == 1
    _click(ctx, "gb-remove-choice")
    _answer(ctx, True)
    assert not any(e["kind"] == "conditional" for e in _edges(ctx))
    assert ctx.eval("inspector().props.node && inspector().props.node.id") == "a", "the panel does not go blank on an index that no longer exists"


def test_after_a_remove_from_the_edge_inspector_no_edge_is_selected(ctx) -> None:
    """B1: the edge's index stayed selected, so the canvas highlighted whichever edge slid into it and a later Delete step opened a connection nobody selected."""
    _mount(ctx, _two_paths(), select="a")
    ctx.eval("canvas().props.onEdgeClick(1); MR.rerender();")
    _click(ctx, "gb-remove-choice")
    _answer(ctx, True)
    assert ctx.eval("inspector().props.edgeIdx === null") is True, "the inspector's edge"
    assert ctx.eval("canvas().props.selectedEdgeId === null") is True, "the canvas's selected edge"


def test_a_question_asked_for_one_choice_is_not_answered_for_another(ctx) -> None:
    """B2: the confirmation followed the inspector to ANOTHER choice, and Remove deleted that one. The question belongs to the choice it was asked about: if the inspector has moved on, or is
    gone, when the answer comes, nothing is deleted."""
    spec = _two_paths()
    spec["edges"].append({"kind": "conditional", "from_node": "w", "router": {"kind": "json_path", "branches": [
        {"conditions": [], "to_node": "m"}, {"conditions": [], "to_node": "m"}, {"conditions": [], "to_node": "m"}], "default_to": "m"}})
    _mount(ctx, spec)
    ctx.eval("canvas().props.onEdgeClick(1); MR.rerender();")
    _click(ctx, "gb-remove-choice")
    assert _asked(ctx) == 1
    ctx.eval("canvas().props.onEdgeClick(6); MR.rerender();")
    n = len(_edges(ctx))
    _answer(ctx, True)
    assert len(_edges(ctx)) == n, "the answer to the question about choice 1 deleted nothing"
    assert _find(ctx, "gb-remove-choice") and _asked(ctx) == 0, "and choice 6 is not asking anything"


def test_a_question_whose_choice_changed_while_it_was_open_deletes_nothing(ctx) -> None:
    """The window keys (undo, redo) work while the dialog is open; if the edge at that index is not the one that was asked about when the answer arrives, the answer is dropped."""
    _mount(ctx, _two_paths())
    _click(ctx, "gb-remove-choice")
    ctx.eval("inspector().props.dispatch({ type: 'UPDATE_EDGE', idx: 1, patch: { router: Object.assign({}, draftOf().edges[1].router, { default_to: 'e' }) } }); MR.rerender();")
    n = len(_edges(ctx))
    _answer(ctx, True)
    assert len(_edges(ctx)) == n


def test_a_choice_with_no_paths_is_removed_at_once(ctx) -> None:
    spec = copy.deepcopy(BASE)
    spec["edges"][1]["router"]["branches"] = []
    _mount(ctx, spec)
    n_edges = len(_edges(ctx))
    _click(ctx, "gb-remove-choice")
    assert len(_edges(ctx)) == n_edges - 1 and _asked(ctx) == 0, "nothing to lose, nothing to ask"


def test_a_callable_router_is_removed_at_once_and_shows_no_control_when_read_only(ctx) -> None:
    spec = copy.deepcopy(BASE)
    spec["edges"][1]["router"] = {"kind": "callable", "callable_id": "pick"}
    _mount(ctx, spec)
    n_edges = len(_edges(ctx))
    _click(ctx, "gb-remove-choice")
    assert len(_edges(ctx)) == n_edges - 1 and _asked(ctx) == 0
    _mount(ctx, spec, read_only=True)
    assert not _find(ctx, "gb-remove-choice")
    ctx.eval("canvas().props.onEdgeClick(1); MR.rerender();")
    assert not _find(ctx, "gb-remove-choice"), "the read-only edge inspector of a callable choice"


def test_two_choices_of_one_step_have_different_names(ctx) -> None:
    """N1: two identical "Remove this choice" buttons on a step are indistinguishable to a screen reader; each is named after the choice it removes."""
    spec = copy.deepcopy(BASE)
    spec["edges"].insert(2, {"kind": "conditional", "from_node": "a", "router": {"kind": "json_path", "branches": [{"conditions": [], "to_node": "t"}], "default_to": "t"}})
    _mount(ctx, spec)
    names = json.loads(ctx.eval("JSON.stringify(MR.findAll('gb-remove-choice').map(function (b) { return b.props['aria-label']; }))"))
    assert len(names) == 2 and names[0] != names[1] and all(n and n.startswith("Remove the choice after") for n in names), names


def test_a_half_typed_value_does_not_slide_into_the_choice_that_follows_a_removed_one(ctx) -> None:
    """B3: the step inspector keys its "What happens next" sections by edge index, so with two adjacent choices the second inherited the first's half-typed text, alert and Save gate
    when the first was removed."""
    spec = copy.deepcopy(BASE)
    spec["edges"][1]["router"]["branches"] = [{"conditions": [{"path": "ok", "op": "eq", "value": {"a": 1}}], "to_node": "f"}]
    spec["edges"].insert(2, {"kind": "conditional", "from_node": "a", "router": {"kind": "json_path", "branches": [
        {"conditions": [{"path": "ok", "op": "eq", "value": {"a": 1}}], "to_node": "t"}], "default_to": "t"}})
    _mount(ctx, spec)
    assert json.loads(ctx.eval("JSON.stringify(boxes().map(function (b) { return b.props.value; }))")) == ['{"a":1}', '{"a":1}']
    ctx.eval("typeInto(0, '{\"a\":1}x');")
    assert _find(ctx, "gb-branch-value-error")
    _click(ctx, "gb-remove-choice")        # the first "Remove this choice" is the first choice's
    _answer(ctx, True)
    assert json.loads(ctx.eval("JSON.stringify(boxes().map(function (b) { return b.props.value; }))")) == ['{"a":1}'], "the remaining choice shows its own stored value"
    assert not _find(ctx, "gb-branch-value-error"), "and nothing is half-typed on it"


def test_a_remove_is_undone_by_undo(ctx) -> None:
    _mount(ctx, _two_paths())
    _click(ctx, "gb-remove-choice")
    _answer(ctx, True)
    n = len(_edges(ctx))
    ctx.eval("pressUndo();")
    assert len(_edges(ctx)) == n + 1


def test_a_static_connection_is_removed_as_before(ctx) -> None:
    _mount(ctx, BASE, select="s")
    ctx.eval("findType(MR.find('gb-builder'), GB_Canvas).props.onEdgeClick(0); MR.rerender();")
    texts = json.loads(ctx.eval("JSON.stringify(MR.texts())"))
    assert "Remove this connection" in texts
    assert not _find(ctx, "gb-remove-choice"), "the choice control is for choices"
