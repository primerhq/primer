"""What ``useFocusTrap`` DOES, driven in V8 against a small fake DOM (console review 2026-10-08, C-028, review of PR 515).

``tests/ui_e2e/test_overlay_focus_journey.py`` drives the trap in a real browser, but that lane does not run for a reviewer without
Playwright, and a journey that only checks "focus stayed inside" cannot tell a wrapping trap from one that never wraps. These tests run the
real ``ui/foundation/focus-trap.js`` against a fake page: a document order of elements, a ``document.activeElement``, a native Tab that
moves along that order unless the handler prevents it, and the two React hooks the trap uses (``useRef``, ``useEffect`` with cleanup).

What they pin, one behaviour each: Tab on the last element wraps to the first and Shift+Tab on the first wraps to the last (without the
wrap the native Tab leaves the dialog and the page behind it is reached); focus moves in on open; focus goes back to the element that had
it when the dialog opened, on close, and not to an element that is gone; an element that already holds focus inside (autoFocus) is kept,
while the opener is still the one restored; a Tab already handled by a nested dialog is left alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

HOOK = (Path(__file__).resolve().parents[2] / "ui" / "foundation" / "focus-trap.js").read_text(encoding="utf-8")

PRELUDE = r"""
var window = {};
var __h = { refs: [], effects: [], i: 0, pending: [] };
var React = {
  useRef: function (init) { var i = __h.i++; if (!(i in __h.refs)) __h.refs[i] = { current: init }; return __h.refs[i]; },
  useEffect: function (fn, deps) { __h.pending.push({ slot: __h.i++, fn: fn, deps: deps }); },
};

// ---- a fake page: elements in document order, native Tab along it ----
var PAGE = [];                       // every tabbable element, in document order
var DOC = [];                        // every element, tabbable or not (a tabindex -1 heading), in document order
var Node = { DOCUMENT_POSITION_PRECEDING: 2, DOCUMENT_POSITION_FOLLOWING: 4, DOCUMENT_POSITION_CONTAINS: 8, DOCUMENT_POSITION_CONTAINED_BY: 16 };
var document = {
  activeElement: null,
  contains: function (el) { return PAGE.indexOf(el) >= 0 || el.__node === true; },
};
function el(name, opts) {
  opts = opts || {};
  var e = { name: name, parent: opts.parent || null, visible: opts.visible !== false, listeners: {}, focusables: [] };
  e.focus = function () { document.activeElement = e; };
  e.contains = function (o) { while (o) { if (o === e) return true; o = o.parent; } return false; };
  // what the browser says of `o` relative to this element: PRECEDING / FOLLOWING in document order, plus CONTAINS / CONTAINED_BY for an ancestor / descendant
  e.compareDocumentPosition = function (o) {
    if (o === e) return 0;
    var flags = DOC.indexOf(o) > DOC.indexOf(e) ? Node.DOCUMENT_POSITION_FOLLOWING : Node.DOCUMENT_POSITION_PRECEDING;
    if (e.contains(o)) flags |= Node.DOCUMENT_POSITION_CONTAINED_BY; else if (o.contains && o.contains(e)) flags |= Node.DOCUMENT_POSITION_CONTAINS;
    return flags;
  };
  e.querySelectorAll = function () { return e.focusables.slice(); };      // which descendants match the selector is the browser's job
  e.addEventListener = function (t, fn) { (e.listeners[t] = e.listeners[t] || []).push(fn); };
  e.removeEventListener = function (t, fn) { e.listeners[t] = (e.listeners[t] || []).filter(function (f) { return f !== fn; }); };
  Object.defineProperty(e, 'offsetParent', { get: function () { return e.visible ? {} : null; } });
  return e;
}
function press(node, shift) {
  // the browser dispatches keydown to the focused element; it reaches the dialog node by bubbling. Then the native default runs.
  var ev = { type: 'keydown', key: 'Tab', shiftKey: !!shift, defaultPrevented: false, preventDefault: function () { this.defaultPrevented = true; } };
  (node.listeners['keydown'] || []).forEach(function (fn) { fn(ev); });
  if (!ev.defaultPrevented) {
    var cur = document.activeElement, i = PAGE.indexOf(cur), next;
    if (i >= 0 || DOC.indexOf(cur) < 0) {
      next = PAGE[(i + (shift ? -1 : 1) + PAGE.length) % PAGE.length];                  // a tab stop: the next one along the page
    } else {
      // parked on an element that is not a tab stop: the browser goes to the next (previous) tab stop from there in document order
      var pos = DOC.indexOf(cur);
      var order = shift ? DOC.slice(0, pos).reverse() : DOC.slice(pos + 1);
      next = order.filter(function (x) { return PAGE.indexOf(x) >= 0; })[0] || PAGE[shift ? PAGE.length - 1 : 0];
    }
    next.focus();
  }
  return ev;
}

// ---- a minimal commit loop for the hook ----
function render(ref, active, opts, deps) {
  __h.i = 0; __h.pending = [];
  window.primerApi.useFocusTrap(ref, active, opts, deps);
}
function commit() {
  __h.pending.forEach(function (p) {
    var prev = __h.effects[p.slot];
    var same = prev && prev.deps.length === p.deps.length && prev.deps.every(function (d, k) { return d === p.deps[k]; });
    if (same) return;
    if (prev && prev.cleanup) prev.cleanup();
    __h.effects[p.slot] = { deps: p.deps, cleanup: p.fn() };
  });
}
function unmount() {
  __h.effects.forEach(function (e) { if (e && e.cleanup) e.cleanup(); });
  __h.effects = []; __h.refs = [];
}

// ---- the scene: opener | dialog(first, middle, last) | after ----
// The dialog box is a tab stop of its own in document order (tabindex -1 makes it focusable, and it precedes its children), so a native
// Shift+Tab from the box goes to the opener, OUTSIDE the dialog: only the trap can send it to the last element.
function scene() {
  PAGE.length = 0;
  var opener = el('opener'), after = el('after');
  var dialog = el('dialog'); dialog.__node = true;
  var first = el('first', { parent: dialog }), middle = el('middle', { parent: dialog }), last = el('last', { parent: dialog });
  dialog.focusables = [first, middle, last];
  [opener, dialog, first, middle, last, after].forEach(function (x) { PAGE.push(x); });
  DOC.length = 0; PAGE.forEach(function (x) { DOC.push(x); });
  document.activeElement = opener;
  return { opener: opener, dialog: dialog, first: first, middle: middle, last: last, after: after };
}
// The same dialog with elements that hold focus but are no tab stop (tabindex -1: a heading a removal hands focus to, a status line, a container),
// before the first tab stop (head), between two (gap), inside the last one (inside) and after it (tail).
function scene2() {
  PAGE.length = 0; DOC.length = 0;
  var opener = el('opener'), after = el('after');
  var dialog = el('dialog'); dialog.__node = true;
  var head = el('head', { parent: dialog }), first = el('first', { parent: dialog }), middle = el('middle', { parent: dialog });
  var gap = el('gap', { parent: dialog }), last = el('last', { parent: dialog }), inside = el('inside', { parent: last }), tail = el('tail', { parent: dialog });
  dialog.focusables = [first, middle, last];
  [opener, dialog, first, middle, last, after].forEach(function (x) { PAGE.push(x); });
  [opener, dialog, head, first, middle, gap, last, inside, tail, after].forEach(function (x) { DOC.push(x); });
  document.activeElement = opener;
  return { opener: opener, dialog: dialog, head: head, first: first, middle: middle, gap: gap, last: last, inside: inside, tail: tail, after: after };
}
function open(s, opts, deps) {
  var ref = { current: s.dialog };
  render(ref, true, opts || null, deps || []);
  return ref;
}
"""


@pytest.fixture()
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval(PRELUDE)
    c.eval(HOOK)
    yield c
    c.close()


def _ev(ctx, code: str):
    return ctx.eval(code)


def test_tab_on_the_last_element_wraps_to_the_first_and_does_not_leave_the_dialog(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      s.last.focus();
      var ev = press(s.dialog, false);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "first"}, "Tab past the last element must come back to the first, not reach the page behind"


def test_shift_tab_on_the_first_element_wraps_to_the_last(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      s.first.focus();
      var ev = press(s.dialog, true);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "last"}


def test_shift_tab_with_focus_on_the_dialog_box_itself_goes_to_the_last_element(ctx) -> None:
    """The dialog node is focusable (tabindex -1): a click on its background puts focus there, and the native Shift+Tab from it would
    walk to whatever precedes the dialog in the page."""
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      s.dialog.focus();
      var ev = press(s.dialog, true);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "last"}, "the trap, not the browser's default, sent focus to the last element"


def test_without_the_trap_a_native_shift_tab_from_the_dialog_box_leaves_the_dialog(ctx) -> None:
    """The control for the test above: the same scene with no listener (a closed trap) walks to the opener, which is the escape the trap
    exists to prevent; if the fake page ever stopped modelling that, the test above would pass for the wrong reason."""
    out = _ev(ctx, """(function () {
      var s = scene(), ref = { current: s.dialog };
      render(ref, false, null, []); commit();
      s.dialog.focus();
      var ev = press(s.dialog, true);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": False, "now": "opener"}


def test_tab_between_the_ends_is_left_to_the_browser(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      s.first.focus();
      var ev = press(s.dialog, false);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": False, "now": "middle"}


def test_focus_moves_into_the_dialog_when_it_opens(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s); commit();
      return document.activeElement.name;
    })()""")
    assert out == "first"


def test_the_initial_option_names_the_element_that_gets_focus(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s, { initial: function (node) { return node.focusables[1]; } }); commit();
      return document.activeElement.name;
    })()""")
    assert out == "middle"


def test_focus_goes_back_to_the_opener_when_the_dialog_closes(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      var inside = document.activeElement.name;
      render(ref, false, null, []); commit();            // closed
      return { inside: inside, after: document.activeElement.name };
    })()""")
    assert out == {"inside": "first", "after": "opener"}, "focus must return to the element that had it when the dialog opened"


def test_focus_goes_back_to_the_opener_when_the_dialog_unmounts(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s); commit();
      unmount();
      return document.activeElement.name;
    })()""")
    assert out == "opener"


def test_an_opener_that_is_gone_is_not_focused(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      PAGE.splice(PAGE.indexOf(s.opener), 1);            // the opener left the document while the dialog was open
      s.middle.focus();
      render(ref, false, null, []); commit();
      return document.activeElement.name;
    })()""")
    assert out == "middle", "focus stays where it was; it is not sent to a detached element"


def test_an_autofocused_input_inside_keeps_focus_but_the_opener_is_still_restored(ctx) -> None:
    """React runs an autoFocus during commit, before any effect: by the time the trap's effect runs, focus is already inside the dialog,
    and the real opener can only have been captured during render."""
    out = _ev(ctx, """(function () {
      var s = scene();
      var ref = { current: s.dialog };
      render(ref, true, null, []);                       // render: the opener (document.activeElement) is captured here
      s.middle.focus();                                  // commit: autoFocus puts focus in the dialog
      commit();                                          // the effect runs
      var kept = document.activeElement.name;
      render(ref, false, null, []); commit();
      return { kept: kept, after: document.activeElement.name };
    })()""")
    assert out == {"kept": "middle", "after": "opener"}


def test_the_opener_is_captured_once_when_the_dialog_becomes_active_not_on_every_render(ctx) -> None:
    """The guard is ``active && !wasActiveRef.current``: while the dialog stays open it renders again and again, and focus is inside it by
    then, so capturing on every render would make the "opener" an element INSIDE the dialog and close would send focus there."""
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();            // opened from the opener; focus moved to the first element
      s.middle.focus();                                     // the user tabs on
      render(ref, true, null, []); commit();                // the dialog re-renders while it is still open
      render(ref, true, null, []); commit();
      render(ref, false, null, []); commit();               // closed
      return document.activeElement.name;
    })()""")
    assert out == "opener", "focus must go back to what had it when the dialog OPENED, not to something that had it while it was open"


def test_a_dialog_reopened_after_closing_captures_its_new_opener(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = open(s); commit();
      render(ref, false, null, []); commit();               // closed: focus back on the opener
      s.after.focus();                                      // the user goes elsewhere and opens the dialog again from there
      render(ref, true, null, []); commit();
      render(ref, false, null, []); commit();
      return document.activeElement.name;
    })()""")
    assert out == "after", "the second opening remembers the second opener"


def test_a_closed_dialog_does_not_trap_and_does_not_steal_focus(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(), ref = { current: s.dialog };
      render(ref, false, null, []); commit();
      s.last.focus();
      var ev = press(s.dialog, false);                   // no listener: the native default runs
      return { prevented: ev.defaultPrevented, now: document.activeElement.name, listeners: (s.dialog.listeners.keydown || []).length };
    })()""")
    assert out == {"prevented": False, "now": "after", "listeners": 0}


def test_a_tab_already_handled_by_a_nested_dialog_is_left_alone(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s); commit();
      s.last.focus();
      var ev = { type: 'keydown', key: 'Tab', shiftKey: false, defaultPrevented: true, preventDefault: function () {} };
      s.dialog.listeners.keydown.forEach(function (fn) { fn(ev); });
      return document.activeElement.name;
    })()""")
    assert out == "last", "a wrap another handler already did must not be wrapped a second time"


def test_other_keys_are_ignored(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); open(s); commit();
      s.last.focus();
      var ev = { type: 'keydown', key: 'a', shiftKey: false, defaultPrevented: false, preventDefault: function () { this.defaultPrevented = true; } };
      s.dialog.listeners.keydown.forEach(function (fn) { fn(ev); });
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": False, "now": "last"}


def test_a_dialog_with_nothing_focusable_keeps_focus_on_the_box_and_swallows_tab(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); s.dialog.focusables = [];
      PAGE.length = 0; [s.opener, s.after].forEach(function (x) { PAGE.push(x); });
      open(s); commit();
      var landed = document.activeElement.name;
      var ev = press(s.dialog, false);
      return { landed: landed, prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"landed": "dialog", "prevented": True, "now": "dialog"}


def test_hidden_elements_are_not_tab_stops_so_the_wrap_goes_to_the_last_visible_one(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene(); s.last.visible = false;           // display:none: offsetParent is null
      PAGE.splice(PAGE.indexOf(s.last), 1);
      open(s); commit();
      s.middle.focus();
      var ev = press(s.dialog, false);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "first"}


def test_the_hook_is_published_for_the_components_that_call_it(ctx) -> None:
    assert _ev(ctx, "typeof window.primerApi.useFocusTrap + '/' + typeof window.primerApi.focusablesOf") == "function/function"


# ---- round 2 of #705 (board task 01a122c8-33c6): a focus parked on an element that is no tab stop ----


def test_tab_from_an_element_after_the_last_tab_stop_wraps_to_the_first(ctx) -> None:
    """The fake page's native Tab from a tabindex -1 element after the last tab stop goes on to the page behind the scrim; the trap wrapped only when focus WAS the last tab stop (or outside)."""
    out = _ev(ctx, """(function () {
      var s = scene2(), ref = open(s); commit();
      s.tail.focus();
      var ev = press(s.dialog, false);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "first"}, "a Tab from after the last tab stop reached the page behind the dialog"


def test_shift_tab_from_an_element_before_the_first_tab_stop_wraps_to_the_last(ctx) -> None:
    out = _ev(ctx, """(function () {
      var s = scene2(), ref = open(s); commit();
      s.head.focus();
      var ev = press(s.dialog, true);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "last"}, "a Shift+Tab from before the first tab stop reached the page in front of the dialog"


def test_tab_from_inside_the_last_tab_stop_wraps_too(ctx) -> None:
    """An element inside the last tab stop (a tabindex -1 span in a button) has nothing after it in the dialog either."""
    out = _ev(ctx, """(function () {
      var s = scene2(), ref = open(s); commit();
      s.inside.focus();
      var ev = press(s.dialog, false);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })()""")
    assert out == {"prevented": True, "now": "first"}


def test_a_parked_element_between_two_tab_stops_is_left_to_the_browser(ctx) -> None:
    """The wrap is for the ends only: between two tab stops the browser's own Tab and Shift+Tab already stay inside."""
    out = _ev(ctx, """(function () {
      var s = scene2(), ref = open(s); commit();
      s.gap.focus();
      var fwd = press(s.dialog, false), after = document.activeElement.name;
      s.gap.focus();
      var back = press(s.dialog, true), before = document.activeElement.name;
      return { fwd: fwd.defaultPrevented, after: after, back: back.defaultPrevented, before: before };
    })()""")
    assert out == {"fwd": False, "after": "last", "back": False, "before": "middle"}


def test_without_the_trap_a_native_tab_from_after_the_last_tab_stop_leaves_the_dialog(ctx) -> None:
    """The control for the three tests above: the same scene with no listener goes to the page behind (Tab) and in front (Shift+Tab); if the fake page stopped modelling that they would pass for the wrong reason."""
    out = _ev(ctx, """(function () {
      var s = scene2(), ref = { current: s.dialog };
      render(ref, false, null, []); commit();
      s.tail.focus(); press(s.dialog, false); var fwd = document.activeElement.name;
      s.head.focus(); press(s.dialog, true); var back = document.activeElement.name;
      return { fwd: fwd, back: back };
    })()""")
    assert out == {"fwd": "after", "back": "dialog"}
