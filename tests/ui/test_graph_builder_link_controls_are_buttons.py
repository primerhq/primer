"""The graph builder's text controls are real buttons, named for what they act on, and the x buttons are big enough to hit (board task 01a12124, found by the #692 review).

Three controls were ``<span onClick>``: "+ Add a path" (the branch builder), "Remove this connection" (the edge inspector) and "Delete this step" (the step inspector, behind an "Advanced" toggle that was
a ``<div onClick>`` itself). A span has no role, no focus and no key handling, so a keyboard or screen-reader user could neither reach nor press them. Now each is a ``<button type="button">`` with a
name that says what it acts on (two choices of one step, two steps with the same name and two connections between the same steps are told apart by the words, as the Remove this choice button is), the two
destructive ones ask first through the console's own ``confirmDialog`` and drop the answer when the question is no longer about what is on screen, and the Advanced toggle says whether it is open
(``aria-expanded``). The x of a condition and of a path were 8 by 13 px; they have a 24 by 24 px box (WCAG 2.2 SC 2.5.8) that does not move the glyph (the extra size is taken back by the margin) and does not reach into the control beside it.

Round 2 (lead's review of #705): the names start with the visible text (WCAG 2.5.3 Label in Name); the Advanced toggle keeps its 11 px text; after a confirmed removal the keyboard lands somewhere that exists (the step's name field, or the 'Nothing selected' heading) instead of the body, where one more Tab left the overlay; the stale-answer guard for a connection is pinned with an undo while the question is open; a connection that is not edge 0 goes alone.

The REAL builder runs in V8 on the strict mini React (``tests/ui/_graph_builder_v8.py``). What the box measures in a browser is in ``tests/ui_e2e/test_builder_link_controls_journey.py``.
"""

from __future__ import annotations

import copy
import json

import pytest

from tests.ui._graph_builder_v8 import DRIVER, PRELUDE, builder_code
from tests.ui._mini_react import mini_react_context
from tests.ui.test_graph_builder_import_shapes import BASE

# the console's confirmDialog, as a promise the test answers: every question asked is kept with its options
CONFIRM = """
var __confirms = [];
var confirmDialog = function (opts) { return new Promise(function (resolve) { __confirms.push({ opts: opts, resolve: resolve }); }); };
"""


# the frames the focus helper waits for, run by the test; a document that records what is asked to take focus
FOCUS = """
var __focused = [];
var __frames = [];
window.requestAnimationFrame = function (fn) { __frames.push(fn); return 0; };
function flushFrames() { for (var i = 0; i < 6 && __frames.length; i++) { var run = __frames.splice(0); run.forEach(function (fn) { fn(); }); } }
var document = { querySelector: function (sel) { return { focus: function () { __focused.push(sel); } }; } };
"""

# Ctrl/Cmd-Z, to undo while a question is open
KEYS = """
var __KEYS = [];
window.addEventListener = function (t, f) { if (t === 'keydown') __KEYS.push(f); };
window.removeEventListener = function (t, f) { var i = __KEYS.indexOf(f); if (i >= 0) __KEYS.splice(i, 1); };
function pressUndo() { __KEYS.slice().forEach(function (f) { f({ key: 'z', metaKey: true, ctrlKey: false, shiftKey: false, preventDefault: function () {} }); }); MR.rerender(); }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(builder_code(), PRELUDE + CONFIRM + FOCUS + KEYS)
    c.eval(DRIVER)
    c.eval("function draftOf() { return inspector().props.draft; }")
    c.eval("function canvas() { return findType(MR.find('gb-builder'), GB_Canvas); }")
    try:
        yield c
    finally:
        c.close()


def _mount(ctx, spec: dict, *, select: str | None = "a", read_only: bool = False) -> None:
    spec = {**spec, **({"harness_id": "managed"} if read_only else {})}
    ctx.eval(f"mountBuilder({json.dumps(spec)});")
    if select:
        ctx.eval(f"selectNode({json.dumps(select)});")


def _edges(ctx) -> list[dict]:
    return json.loads(ctx.eval("JSON.stringify(draftOf().edges)"))


def _node_ids(ctx) -> list[str]:
    return json.loads(ctx.eval("JSON.stringify(draftOf().nodes.map(function (n) { return n.id; }))"))


def _info(ctx, testid: str) -> dict | None:
    """What the element with this test id is: its DOM tag, its ``type`` attribute, its accessible name (``aria-label``, or the text it holds) and ``aria-expanded``."""
    return json.loads(ctx.eval(
        "(function () { var el = MR.find(" + json.dumps(testid) + "); if (!el) return 'null';"
        " return JSON.stringify({ tag: String(el.type), buttonType: el.props.type || null, name: el.props['aria-label'] || null, expanded: el.props['aria-expanded'] === undefined ? null : el.props['aria-expanded'] }); })()"
    ))


def _press(ctx, testid: str) -> None:
    ctx.eval(f"MR.click({json.dumps(testid)}); MR.rerender();")


def _answer(ctx, ok: bool) -> dict:
    """Answer the oldest question put to confirmDialog and redraw; returns the options it was asked with."""
    opts = json.loads(ctx.eval("JSON.stringify(__confirms[0].opts)"))
    ctx.eval(f"__confirms.shift().resolve({'true' if ok else 'false'});")
    ctx.eval("void 0;")
    ctx.eval("MR.rerender(); flushFrames();")
    return opts


def _asked(ctx) -> int:
    return ctx.eval("__confirms.length")


def _select_edge(ctx, idx: int) -> None:
    ctx.eval(f"canvas().props.onEdgeClick({idx}); MR.rerender();")


def _open_advanced(ctx) -> None:
    _press(ctx, "gb-advanced-toggle")


def _selected_node(ctx) -> str | None:
    return ctx.eval("inspector().props.node ? inspector().props.node.id : null")


# ---- + Add a path -------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_add_a_path_is_a_button_that_says_which_choice_it_adds_to_and_adds_a_path(ctx) -> None:
    _mount(ctx, BASE)
    info = _info(ctx, "gb-add-path")
    assert info and info["tag"] == "button" and info["buttonType"] == "button", info
    assert info["name"] == "Add a path to the choice after Ask (first path goes to Split)", info
    before = len(next(e for e in _edges(ctx) if e["kind"] == "conditional")["router"]["branches"])
    _press(ctx, "gb-add-path")
    assert len(next(e for e in _edges(ctx) if e["kind"] == "conditional")["router"]["branches"]) == before + 1


def test_two_choices_of_one_step_have_two_add_a_path_buttons_with_different_names(ctx) -> None:
    spec = copy.deepcopy(BASE)
    spec["edges"].insert(2, {"kind": "conditional", "from_node": "a", "router": {"kind": "json_path", "branches": [{"conditions": [], "to_node": "t"}], "default_to": "t"}})
    _mount(ctx, spec)
    names = json.loads(ctx.eval("JSON.stringify(MR.findAll('gb-add-path').map(function (b) { return b.props['aria-label']; }))"))
    assert len(names) == 2 and names[0] != names[1] and all(n.startswith("Add a path to the choice after Ask") for n in names), names


def test_a_read_only_builder_has_no_add_a_path(ctx) -> None:
    _mount(ctx, BASE, read_only=True)
    assert _info(ctx, "gb-add-path") is None


# ---- Remove this connection ---------------------------------------------------------------------------------------------------------------------------------------------------------


def test_remove_this_connection_is_a_button_named_for_the_two_steps_it_joins(ctx) -> None:
    """N5: the name STARTS with the visible text (WCAG 2.5.3 Label in Name, Level A), so a voice user who says what they see is understood."""
    _mount(ctx, BASE, select=None)
    _select_edge(ctx, 0)
    info = _info(ctx, "gb-remove-connection")
    assert info and info["tag"] == "button" and info["buttonType"] == "button", info
    assert info["name"] == "Remove this connection from Start to Ask", info


def test_removing_a_connection_asks_first_and_keep_leaves_it(ctx) -> None:
    _mount(ctx, BASE, select=None)
    _select_edge(ctx, 0)
    n_edges = len(_edges(ctx))
    _press(ctx, "gb-remove-connection")
    assert _asked(ctx) == 1 and len(_edges(ctx)) == n_edges, "asked, and nothing removed before the answer"
    opts = _answer(ctx, False)
    assert opts["title"] == "Remove this connection?" and opts["danger"] is True
    assert "Start" in opts["message"] and "Ask" in opts["message"] and opts["confirmLabel"] == "Remove" and opts["cancelLabel"] == "Keep"
    assert len(_edges(ctx)) == n_edges


def test_removing_a_connection_when_confirmed_takes_that_edge_away_and_selects_the_step_it_left(ctx) -> None:
    _mount(ctx, BASE, select=None)
    _select_edge(ctx, 0)
    n_edges = len(_edges(ctx))
    _press(ctx, "gb-remove-connection")
    _answer(ctx, True)
    edges = _edges(ctx)
    assert len(edges) == n_edges - 1 and not any(e["kind"] == "static" and e["from_node"] == "s" and e["to_node"] == "a" for e in edges)
    assert _selected_node(ctx) == "s", "the panel does not go blank on an edge index that no longer exists"


def test_an_answer_about_a_connection_is_dropped_when_another_edge_is_on_screen(ctx) -> None:
    _mount(ctx, BASE, select=None)
    _select_edge(ctx, 0)
    n_edges = len(_edges(ctx))
    _press(ctx, "gb-remove-connection")
    _select_edge(ctx, 2)                                                # the inspector moved on to another connection while the question was open
    _answer(ctx, True)
    assert len(_edges(ctx)) == n_edges, "the 'yes' was about the first connection"


def test_a_read_only_builder_has_no_remove_this_connection(ctx) -> None:
    _mount(ctx, BASE, select=None, read_only=True)
    _select_edge(ctx, 0)
    assert _info(ctx, "gb-remove-connection") is None


# ---- Delete this step ---------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_advanced_toggle_is_a_button_that_says_whether_it_is_open(ctx) -> None:
    _mount(ctx, BASE, select="t")
    closed = _info(ctx, "gb-advanced-toggle")
    assert closed and closed["tag"] == "button" and closed["buttonType"] == "button" and closed["expanded"] is False, closed
    _open_advanced(ctx)
    assert _info(ctx, "gb-advanced-toggle")["expanded"] is True
    assert _info(ctx, "gb-delete-step") is not None, "Delete this step is behind it"


def test_delete_this_step_is_a_button_named_for_the_step(ctx) -> None:
    """N5: the visible text first, then what it acts on."""
    _mount(ctx, BASE, select="t")
    _open_advanced(ctx)
    info = _info(ctx, "gb-delete-step")
    assert info and info["tag"] == "button" and info["buttonType"] == "button", info
    assert info["name"] == "Delete this step: Tool", info


def test_deleting_a_step_asks_first_and_keep_leaves_it(ctx) -> None:
    _mount(ctx, BASE, select="t")
    _open_advanced(ctx)
    before = _node_ids(ctx)
    _press(ctx, "gb-delete-step")
    assert _asked(ctx) == 1 and _node_ids(ctx) == before
    opts = _answer(ctx, False)
    assert opts["title"] == "Delete this step?" and opts["danger"] is True and "Tool" in opts["message"] and "connection" in opts["message"]
    assert opts["confirmLabel"] == "Delete" and opts["cancelLabel"] == "Keep"
    assert _node_ids(ctx) == before


def test_deleting_a_step_when_confirmed_takes_it_and_its_connections_away(ctx) -> None:
    _mount(ctx, BASE, select="t")
    _open_advanced(ctx)
    _press(ctx, "gb-delete-step")
    _answer(ctx, True)
    assert "t" not in _node_ids(ctx)
    assert not any(e.get("from_node") == "t" or e.get("to_node") == "t" for e in _edges(ctx))


def test_an_answer_about_a_step_is_dropped_when_another_step_is_on_screen(ctx) -> None:
    _mount(ctx, BASE, select="t")
    _open_advanced(ctx)
    before = _node_ids(ctx)
    _press(ctx, "gb-delete-step")
    ctx.eval("selectNode('w');")                                        # the inspector moved on to another step while the question was open
    _answer(ctx, True)
    assert _node_ids(ctx) == before


def test_a_read_only_builder_has_no_delete_this_step(ctx) -> None:
    _mount(ctx, BASE, select="t", read_only=True)
    _open_advanced(ctx)
    assert _info(ctx, "gb-delete-step") is None


# ---- the x buttons ---------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_x_of_a_condition_and_of_a_path_have_a_24_by_24_box_that_does_not_move_the_glyph(ctx) -> None:
    """SC 2.5.8: the box is 24 px each way; the margin takes back what the box adds around the glyph, so the row does not get taller or wider."""
    spec = copy.deepcopy(BASE)
    spec["edges"][1]["router"]["branches"].append({"conditions": [{"path": "ok", "op": "eq", "value": False}], "to_node": "t"})
    _mount(ctx, spec)
    for label in ("Branch 1, condition 1: remove", "Branch 1: remove"):
        style = json.loads(ctx.eval(
            "(function () { var hit = null; (function w(n) { if (hit || n == null || typeof n !== 'object') return; if (Array.isArray(n)) { n.forEach(w); return; } if (!n.__el) return;"
            f" if (n.props && n.props['aria-label'] === {json.dumps(label)}) hit = n;"
            " else if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children); })(MR.find('gb-builder')); return JSON.stringify(hit.props.style); })()"))
        assert style["width"] >= 24 and style["height"] >= 24, (label, style)
        assert style["marginTop"] < 0 and style["marginBottom"] < 0 and style["marginRight"] < 0, (label, "the margin takes back the extra box", style)
        assert "margin" not in style, (label, "longhands only: React warns when a style mixes the shorthand with a longhand", style)


# ---- round 2 (lead's review of #705) ---------------------------------------------------------------------------------------------------------------------------------------------


def _style(ctx, testid: str) -> dict:
    return json.loads(ctx.eval(f"JSON.stringify(MR.find({json.dumps(testid)}).props.style)"))


def test_the_path_x_does_not_reach_into_the_control_beside_it(ctx) -> None:
    """B1: the row's gap is 6; a 24 px box with a margin of -8 reached 2 px into '+ condition', and a click on its right edge removed the path unasked. The negative margin may not exceed the gap."""
    spec = copy.deepcopy(BASE)
    spec["edges"][1]["router"]["branches"].append({"conditions": [{"path": "ok", "op": "eq", "value": False}], "to_node": "t"})
    _mount(ctx, spec)
    style = json.loads(ctx.eval(
        "(function () { var hit = null; (function w(n) { if (hit || n == null || typeof n !== 'object') return; if (Array.isArray(n)) { n.forEach(w); return; } if (!n.__el) return;"
        " if (n.props && n.props['aria-label'] === 'Branch 1: remove') hit = n;"
        " else if (typeof n.type === 'function' && n.type !== React.Fragment) w(n.out); else w(n.children); })(MR.find('gb-builder')); return JSON.stringify(hit.props.style); })()"))
    assert style["marginLeft"] >= -6, style


def test_the_advanced_toggle_keeps_its_11_px_text(ctx) -> None:
    """B2: ``font: "inherit"`` after ``fontSize`` reset the size to 13 px. Buttons already inherit the font (ui/styles.css), so the style names no ``font`` at all."""
    _mount(ctx, BASE, select="t")
    style = _style(ctx, "gb-advanced-toggle")
    assert style["fontSize"] == "var(--fs-11)" and "font" not in style, style


def test_the_advanced_triangle_is_not_read_aloud(ctx) -> None:
    """N8: the triangle is a picture of the state that ``aria-expanded`` already says."""
    _mount(ctx, BASE, select="t")
    assert ctx.eval(
        "(function () { var t = MR.find('gb-advanced-toggle'); return (t.children || []).some(function (c) { return c && c.__el && c.props && c.props['aria-hidden'] === 'true'; }); })()") is True


def test_the_nothing_selected_heading_can_take_focus(ctx) -> None:
    """B3: where the keyboard lands after a step is deleted. A heading takes focus only with ``tabIndex={-1}``."""
    _mount(ctx, BASE, select=None)
    info = json.loads(ctx.eval(
        "(function () { var el = MR.find('gb-nothing-selected'); return el ? JSON.stringify({ tabIndex: el.props.tabIndex }) : 'null'; })()"))
    assert info == {"tabIndex": -1}, info


def test_after_a_confirmed_removal_of_a_connection_the_focus_goes_to_the_step_name(ctx) -> None:
    """B3: the removed button is gone and the focus would fall to the body (and one more Tab leaves the overlay). It goes to the name field of the step the panel now shows."""
    _mount(ctx, BASE, select=None)
    _select_edge(ctx, 0)
    _press(ctx, "gb-remove-connection")
    assert json.loads(ctx.eval("JSON.stringify(__focused)")) == []
    _answer(ctx, True)
    assert json.loads(ctx.eval("JSON.stringify(__focused)")) == ['[data-testid="gb-inspector-title"]']


def test_after_a_confirmed_deletion_of_a_step_the_focus_goes_to_the_nothing_selected_heading(ctx) -> None:
    _mount(ctx, BASE, select="t")
    _open_advanced(ctx)
    _press(ctx, "gb-delete-step")
    _answer(ctx, True)
    assert json.loads(ctx.eval("JSON.stringify(__focused)")) == ['[data-testid="gb-nothing-selected"]']


@pytest.mark.parametrize("what", ["connection", "step"])
def test_keeping_moves_the_focus_nowhere(ctx, what: str) -> None:
    """The dialog hands focus back to the button that opened it; the helper does not interfere."""
    if what == "connection":
        _mount(ctx, BASE, select=None)
        _select_edge(ctx, 0)
        _press(ctx, "gb-remove-connection")
    else:
        _mount(ctx, BASE, select="t")
        _open_advanced(ctx)
        _press(ctx, "gb-delete-step")
    _answer(ctx, False)
    assert json.loads(ctx.eval("JSON.stringify(__focused)")) == []


def test_after_removing_a_choice_the_focus_goes_to_the_step_name_whether_it_asked_or_not(ctx) -> None:
    """B3 for #692's Remove this choice, the same class and the same file: with paths it asks first, a choice with none goes at once; either way its button is gone."""
    _mount(ctx, BASE)
    _press(ctx, "gb-remove-choice")
    _answer(ctx, True)
    assert json.loads(ctx.eval("JSON.stringify(__focused)")) == ['[data-testid="gb-inspector-title"]']
    ctx.eval("__focused.length = 0;")
    spec = copy.deepcopy(BASE)
    spec["edges"][1]["router"]["branches"] = []
    _mount(ctx, spec)
    _press(ctx, "gb-remove-choice")
    ctx.eval("flushFrames();")
    assert _asked(ctx) == 0 and json.loads(ctx.eval("JSON.stringify(__focused)")) == ['[data-testid="gb-inspector-title"]']


def test_an_answer_about_a_connection_is_dropped_when_an_undo_put_another_connection_at_that_place(ctx) -> None:
    """N2: the edge-at-index guard. Edge 0 is retargeted (s to a becomes s to t), the question is asked about THAT connection, and Ctrl+Z while it is open puts the old edge 0 (s to a) back at the same
    index: a 'yes' must not remove a connection nobody asked about."""
    _mount(ctx, BASE, select=None)
    ctx.eval("inspector().props.dispatch({ type: 'UPDATE_EDGE', idx: 0, patch: { to_node: 't' } }); MR.rerender();")
    _select_edge(ctx, 0)
    assert _edges(ctx)[0]["to_node"] == "t"
    n_edges = len(_edges(ctx))
    _press(ctx, "gb-remove-connection")
    ctx.eval("pressUndo();")
    assert _edges(ctx)[0]["to_node"] == "a", "the undo put the old edge back"
    _answer(ctx, True)
    assert len(_edges(ctx)) == n_edges and _edges(ctx)[0]["to_node"] == "a", "the 'yes' was about the connection to t"


def test_removing_a_connection_that_is_not_edge_zero_removes_that_one_alone(ctx) -> None:
    """N3: the index the dispatch carries is the edge's own, not a constant."""
    _mount(ctx, BASE, select=None)
    before = _edges(ctx)
    victim = next(i for i, e in enumerate(before) if e["kind"] == "static" and e["from_node"] == "w" and e["to_node"] == "m")
    assert victim > 0
    _select_edge(ctx, victim)
    _press(ctx, "gb-remove-connection")
    _answer(ctx, True)
    after = _edges(ctx)
    assert len(after) == len(before) - 1
    assert not any(e["kind"] == "static" and e["from_node"] == "w" and e["to_node"] == "m" for e in after)
    assert after[0] == before[0], "edge 0 stays"


def test_the_delete_step_guard_is_the_key_not_a_second_id_check() -> None:
    """N4: ``GB_DeleteStep`` is keyed by the node id, so another step is another instance and ``live`` says it; a comparison of ids beside it could never be false."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "ui" / "components" / "graph-builder" / "gb-inspector.jsx").read_text(encoding="utf-8")
    body = src[src.index("function GB_DeleteStep("):src.index("function GB_Inspector(")]
    assert "current.current.id" not in body and "key={node.id}" in src


def test_the_files_that_call_confirm_dialog_declare_it_a_global() -> None:
    """N10: the ``/* global */`` header of each builder file names what it uses from the console's shared scripts."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "ui" / "components" / "graph-builder"
    for name in ("gb-inspector.jsx", "gb-branches.jsx"):
        header = (root / name).read_text(encoding="utf-8").split("\n", 1)[0]
        assert "confirmDialog" in header, (name, header)
