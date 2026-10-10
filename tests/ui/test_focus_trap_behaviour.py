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

import json
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
  e.focus = function () {
    document.activeElement = e;
    var ev = { type: 'focusin', target: e };
    for (var n = e; n; n = n.parent) (n.listeners.focusin || []).slice().forEach(function (fn) { fn(ev); });
    ((document.listeners || {}).focusin || []).slice().forEach(function (fn) { fn(ev); });
  };
  e.contains = function (o) { while (o) { if (o === e) return true; o = o.parent; } return false; };
  // what the browser says of `o` relative to this element: PRECEDING / FOLLOWING in document order, plus CONTAINS / CONTAINED_BY for an ancestor / descendant
  e.compareDocumentPosition = function (o) {
    if (o === e) return 0;
    var flags = DOC.indexOf(o) > DOC.indexOf(e) ? Node.DOCUMENT_POSITION_FOLLOWING : Node.DOCUMENT_POSITION_PRECEDING;
    if (e.contains(o)) flags |= Node.DOCUMENT_POSITION_CONTAINED_BY; else if (o.contains && o.contains(e)) flags |= Node.DOCUMENT_POSITION_CONTAINS;
    return flags;
  };
  e.querySelectorAll = function () { return e.focusables.slice(); };      // which descendants match the selector is the browser's job
  e.matches = function () { return true; };                                // a descendant of a plain dialog is a native stop
  e.closest = function () { return null; };
  e.getAttribute = function () { return null; };
  e.getClientRects = function () { return e.visible ? [{}] : []; };
  e.addEventListener = function (t, fn) { (e.listeners[t] = e.listeners[t] || []).push(fn); };
  e.removeEventListener = function (t, fn) { e.listeners[t] = (e.listeners[t] || []).filter(function (f) { return f !== fn; }); };
  Object.defineProperty(e, 'offsetParent', { get: function () { return e.visible ? {} : null; }, configurable: true });
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


# ---- round 3 (board task 01a124ad): a fake page that knows ATTRIBUTES, the selector, visibility, radio groups, <body>, document-level listeners and several traps at once ----
# The selector the hook hands to querySelectorAll is EVALUATED here (the clauses the hook uses: tag, [attr], [attr="v"], [attr^="v"] and :not([...]) of those), so a clause that forgets
# tabindex="-1" or leaves out contenteditable fails a test, not a grep of the source. What the browser would TAB to is decided by nativeStop, independently of the selector.
PRELUDE_DOM = r"""
document.body = el('body');
document.listeners = {};
document.addEventListener = function (t, fn) { (document.listeners[t] = document.listeners[t] || []).push(fn); };
document.removeEventListener = function (t, fn) { document.listeners[t] = (document.listeners[t] || []).filter(function (f) { return f !== fn; }); };
document.contains = function (x) { return PAGE.indexOf(x) >= 0 || x.__node === true || (x.__ne === true && DOC.indexOf(x) >= 0); };      // scene3's elements are in the document while they are in DOC
var getComputedStyle = function (e) { return { visibility: e.cssVisibility || 'visible' }; };

function parseClause(c) {
  var m = /^\s*([a-z]*)(.*)$/.exec(c), conds = [], r, re = /(:not\()?\[([a-z-]+)(\^?=)?(?:"([^"]*)")?\]\)?/g;
  while ((r = re.exec(m[2]))) conds.push({ not: !!r[1], attr: r[2], op: r[3] || null, val: r[4] });
  return { tag: m[1], conds: conds, notDisabled: m[2].indexOf(':not(:disabled)') >= 0 };
}
// :disabled is the attribute on the control, or <fieldset disabled> above it (the attribute alone does not see the second)
function isDisabled(e) { for (var n = e; n; n = n.parent) { if (n.attrs && 'disabled' in n.attrs && (n === e || n.tag === 'fieldset')) return true; } return false; }
function firstOfType(e) { return DOC.filter(function (x) { return x.parent === e.parent && x.tag === e.tag; })[0] === e; }
function matches(e, sel) {
  return sel.split(',').some(function (c) {
    if (c.indexOf('>') >= 0) {                                    // "details > summary:first-of-type" + the clauses after it
      var parts = c.split('>'), rest = parts[1];
      if (!e.parent || e.parent.tag !== parts[0].trim()) return false;
      if (rest.indexOf(':first-of-type') >= 0 && !firstOfType(e)) return false;
      c = rest.replace(':first-of-type', '');
    }
    var p = parseClause(c);
    if (p.tag && e.tag !== p.tag) return false;
    if (p.notDisabled && isDisabled(e)) return false;
    return p.conds.every(function (k) {
      var has = k.attr in e.attrs, v = e.attrs[k.attr], ok = !has ? false : !k.op ? true : k.op === '=' ? String(v) === k.val : String(v).indexOf(k.val) === 0;
      return k.not ? !ok : ok;
    });
  });
}
// what the browser tabs to, whatever the selector says
function nativeStop(e) {
  var a = e.attrs, t = e.tag;
  if (!e.visible || e.cssVisibility === 'hidden' || e.cssVisibility === 'collapse' || isDisabled(e)) return false;
  for (var up = e; up; up = up.parent) { if (up.attrs && 'inert' in up.attrs) return false; }
  if ('tabindex' in a && isFinite(parseInt(a.tabindex, 10))) return parseInt(a.tabindex, 10) >= 0;      // tabindex="" is no tabindex: the element is a stop by what it is
  if (t === 'summary') return !!e.parent && e.parent.tag === 'details' && firstOfType(e);
  if (t === 'input' && String(a.type).toLowerCase() === 'radio') {
    var group = DOC.filter(function (x) { return x.tag === 'input' && String(x.attrs.type).toLowerCase() === 'radio' && x.attrs.name === a.name; });
    var checked = group.filter(function (x) { return x.checked; })[0];
    return checked ? e === checked : e === group[0];          // one stop per group: the checked one, else the first
  }
  if (t === 'a') return 'href' in a;
  if (['button', 'input', 'select', 'textarea', 'summary', 'iframe'].indexOf(t) >= 0) return true;
  if ((t === 'audio' || t === 'video') && 'controls' in a) return true;
  return 'contenteditable' in a && a.contenteditable !== 'false';
}
function ne(name, spec, parent) {
  var e = el(name, { parent: parent, visible: spec.visible });
  e.__ne = true; e.tag = spec.tag || 'div'; e.attrs = spec.attrs || {}; e.checked = !!spec.checked; e.cssVisibility = spec.cssVisibility || null;
  // Chromium's checkVisibility() with NO options reports visibility:hidden (and collapse) as visible: only { visibilityProperty: true } looks at the property
  if (ne.modern !== false) e.checkVisibility = function (o) { o = o || {}; return e.visible && !(o.visibilityProperty && (e.cssVisibility === 'hidden' || e.cssVisibility === 'collapse')); };
  e.getClientRects = function () { return e.visible ? [{}] : []; };
  if (spec.cssPosition === 'fixed') Object.defineProperty(e, 'offsetParent', { get: function () { return null; }, configurable: true });      // a fixed element has no offsetParent and is shown
  Object.defineProperty(e, 'type', { get: function () { return e.tag === 'input' ? String(e.attrs.type || 'text').toLowerCase() : e.attrs.type; } });         // the normalised value, not the attribute
  e.getAttribute = function (k) { return k in e.attrs ? e.attrs[k] : null; };
  e.querySelectorAll = function (sel) { return DOC.filter(function (x) { return x !== e && e.contains(x) && matches(x, sel); }); };
  e.matches = function (sel) { return matches(e, sel); };
  e.closest = function (sel) { var attr = /^\[([a-z-]+)\]$/.exec(sel)[1]; for (var n = e; n; n = n.parent) { if (n.attrs && attr in n.attrs) return n; } return null; };
  return e;
}
// opener | dialog(specs...) | after, with PAGE decided by nativeStop. specs: [{ name, tag, attrs, checked, cssVisibility, visible }]
function scene3(specs) {
  DOC.length = 0; PAGE.length = 0;
  var opener = ne('opener', { tag: 'button' }), after = ne('after', { tag: 'button' });
  var dialog = ne('dialog', { attrs: { tabindex: '-1' } }); dialog.__node = true;
  var s = { opener: opener, dialog: dialog, after: after };
  DOC.push(opener, dialog);
  specs.forEach(function (sp) { s[sp.name] = ne(sp.name, sp, sp.within ? s[sp.within] : dialog); DOC.push(s[sp.name]); });
  DOC.push(after);
  DOC.forEach(function (x) { if (nativeStop(x)) PAGE.push(x); });
  document.activeElement = opener;
  return s;
}
// the key goes to the focused element, bubbles through its ancestors and then the document; then the native default runs
function nativeTab(shift) {
  var cur = document.activeElement, i = PAGE.indexOf(cur), next;
  if (cur === document.body) next = shift ? PAGE[PAGE.length - 1] : PAGE[0];
  else if (i >= 0) next = PAGE[(i + (shift ? -1 : 1) + PAGE.length) % PAGE.length];
  else {
    var pos = DOC.indexOf(cur), order = shift ? DOC.slice(0, pos).reverse() : DOC.slice(pos + 1);
    next = order.filter(function (x) { return PAGE.indexOf(x) >= 0; })[0] || PAGE[shift ? PAGE.length - 1 : 0];
  }
  next.focus();
}
function pressAt(shift) {
  var ev = { type: 'keydown', key: 'Tab', shiftKey: !!shift, defaultPrevented: false, preventDefault: function () { this.defaultPrevented = true; } };
  for (var n = document.activeElement; n; n = n.parent) (n.listeners.keydown || []).slice().forEach(function (fn) { fn(ev); });
  (document.listeners.keydown || []).slice().forEach(function (fn) { fn(ev); });
  if (!ev.defaultPrevented) nativeTab(shift);
  return ev;
}
function settle() { PAGE.length = 0; DOC.forEach(function (x) { if (nativeStop(x)) PAGE.push(x); }); }       // after a test edits DOC by hand
function inst() { return { refs: [], effects: [], i: 0, pending: [] }; }       // one hook instance per dialog: __h = inst() before its render / commit
"""


@pytest.fixture()
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval(PRELUDE)
    c.eval(PRELUDE_DOM)
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


# ---- round 3 (board task 01a124ad, measured in the review of #717): what the trap treats as a stop, and a focus that is on <body> ----


def _tabs(ctx, specs: str, setup: str, *, modern: bool = True) -> dict:
    """Build a dialog from ``specs`` (the fake page decides what the browser tabs to), open the trap, run ``setup`` (which may move focus) and press Tab and then Shift+Tab from where focus is."""
    return json.loads(ctx.eval(f"""JSON.stringify((function () {{
      unmount(); __h = inst();                                  // a helper call is a new dialog: the trap of the previous call is closed
      ne.modern = {str(modern).lower()};
      var s = scene3({specs}), ref = {{ current: s.dialog }};
      render(ref, true, null, []); commit();
      {setup}
      var at = document.activeElement.name;
      var fwd = pressAt(false), toFwd = document.activeElement.name;
      {setup}
      var back = pressAt(true), toBack = document.activeElement.name;
      return {{ at: at, fwd: fwd.defaultPrevented, toFwd: toFwd, back: back.defaultPrevented, toBack: toBack }};
    }})())"""))


def test_tab_with_focus_on_the_body_goes_into_the_dialog(ctx) -> None:
    """T1, a live leak: the trap listened on the dialog node, and a key whose target is ``<body>`` never reaches it. A focused button that is disabled (a busy submit) or removed hands focus to ``<body>``; the next
    Tab then went to the first control of the page behind the scrim. The same for a focus that sits on the page behind the scrim."""
    behind = _tabs(ctx, "[{name:'first',tag:'button'},{name:'last',tag:'button'}]", "s.opener.focus();")
    assert behind["fwd"] is True and behind["toFwd"] == "first", behind                  # a focus on the page behind the scrim: nothing to continue from, so the ends
    assert behind["back"] is True and behind["toBack"] == "last", behind
    body = _tabs(ctx, "[{name:'first',tag:'button'},{name:'last',tag:'button'}]", "document.activeElement = document.body;")
    assert body["fwd"] is True and body["toFwd"] == "last", body                          # <body> after focus was inside: on from where it was (the trap gave focus to `first`), as the browser would
    assert body["back"] is True and body["toBack"] == "first", body                       # the helper loses focus again after the Tab, which had moved it to `last`: back from there


def test_without_the_trap_a_tab_from_the_body_goes_to_the_page_behind(ctx) -> None:
    """The control for the test above: with the trap closed the same Tab reaches the opener, so the fake page models the leak."""
    out = json.loads(ctx.eval("""JSON.stringify((function () {
      var s = scene3([{name:'first',tag:'button'},{name:'last',tag:'button'}]), ref = { current: s.dialog };
      render(ref, false, null, []); commit();
      document.activeElement = document.body;
      var ev = pressAt(false);
      return { prevented: ev.defaultPrevented, now: document.activeElement.name };
    })())"""))
    assert out == {"prevented": False, "now": "opener"}


def test_with_two_dialogs_open_a_tab_from_the_body_goes_into_the_top_one_and_when_it_closes_into_the_one_below(ctx) -> None:
    out = json.loads(ctx.eval("""JSON.stringify((function () {
      DOC.length = 0;
      var opener = ne('opener', { tag: 'button' });
      var A = ne('A', { attrs: { tabindex: '-1' } }); A.__node = true; var a1 = ne('a1', { tag: 'button' }, A), a2 = ne('a2', { tag: 'button' }, A);
      var B = ne('B', { attrs: { tabindex: '-1' } }); B.__node = true; var b1 = ne('b1', { tag: 'button' }, B), b2 = ne('b2', { tag: 'button' }, B);
      DOC.push(opener, A, a1, a2, B, b1, b2); settle(); document.activeElement = opener;
      var hA = inst(), hB = inst(), refA = { current: A }, refB = { current: B };
      __h = hA; render(refA, true, null, []); commit();
      __h = hB; render(refB, true, null, []); commit();                       // B opens after A: it is on top
      document.activeElement = document.body; pressAt(false); var top = document.activeElement.name;
      b2.focus(); pressAt(false); var wrapped = document.activeElement.name;   // a Tab inside the top dialog is its own, and only its
      __h = hB; render(refB, false, null, []); commit();                       // B closes
      document.activeElement = document.body; pressAt(false); var below = document.activeElement.name;
      return { top: top, wrapped: wrapped, below: below };
    })())"""))
    assert out == {"top": "b2", "wrapped": "b1", "below": "a2"}, out        # each continues from the stop its focus was on (the trap gave focus to b1, and to a1 again when B closed)


@pytest.mark.parametrize("modern", [True, False], ids=["checkVisibility", "offsetParent-and-computed-style"])
def test_a_visibility_hidden_control_is_not_a_tab_stop(ctx, modern: bool) -> None:
    """T2: ``visibility:hidden`` leaves ``offsetParent`` set, so such a control at an end became ``last`` and a Tab from the real last stop left the dialog."""
    end = _tabs(ctx, "[{name:'first',tag:'button'},{name:'last',tag:'button'},{name:'ghost',tag:'button',cssVisibility:'hidden'}]", "s.last.focus();", modern=modern)
    assert end["fwd"] is True and end["toFwd"] == "first", end
    start = _tabs(ctx, "[{name:'ghost',tag:'button',cssVisibility:'hidden'},{name:'first',tag:'button'},{name:'last',tag:'button'}]", "s.first.focus();", modern=modern)
    assert start["back"] is True and start["toBack"] == "last", start


@pytest.mark.parametrize("tag", ["button", "input", "select", "textarea", "a"])
def test_a_control_with_tabindex_minus_one_is_not_a_tab_stop(ctx, tag: str) -> None:
    """T3: only the bare ``[tabindex]`` clause excluded -1; a ``button``, ``input``, ``select``, ``textarea`` or ``a[href]`` with ``tabindex="-1"`` was a stop, and at an end it became ``last``/``first``."""
    attrs = "{tabindex:'-1', href:'#'}" if tag == "a" else "{tabindex:'-1'}"
    end = _tabs(ctx, f"[{{name:'first',tag:'button'}},{{name:'last',tag:'button'}},{{name:'skipped',tag:'{tag}',attrs:{attrs}}}]", "s.last.focus();")
    assert end["fwd"] is True and end["toFwd"] == "first", end
    start = _tabs(ctx, f"[{{name:'skipped',tag:'{tag}',attrs:{attrs}}},{{name:'first',tag:'button'}},{{name:'last',tag:'button'}}]", "s.first.focus();")
    assert start["back"] is True and start["toBack"] == "last", start


def test_a_radio_group_is_one_tab_stop_the_checked_one(ctx) -> None:
    """T4: every radio counted as a stop, so a group at an end with another radio checked let Tab (or Shift+Tab) out: the browser stops once in a group, on the checked radio."""
    radios = "{name:'r1',tag:'input',attrs:{type:'radio',name:'g'}},{name:'r2',tag:'input',attrs:{type:'radio',name:'g'},checked:true},{name:'r3',tag:'input',attrs:{type:'radio',name:'g'}}"
    end = _tabs(ctx, f"[{{name:'first',tag:'button'}},{radios}]", "s.r2.focus();")
    assert end["fwd"] is True and end["toFwd"] == "first", end
    start = _tabs(ctx, f"[{radios},{{name:'last',tag:'button'}}]", "s.r2.focus();")
    assert start["back"] is True and start["toBack"] == "last", start


def test_a_radio_group_with_none_checked_is_one_tab_stop_its_first(ctx) -> None:
    end = _tabs(ctx, "[{name:'first',tag:'button'},{name:'r1',tag:'input',attrs:{type:'radio',name:'g'}},{name:'r2',tag:'input',attrs:{type:'radio',name:'g'}}]", "s.r1.focus();")
    assert end["fwd"] is True and end["toFwd"] == "first", end


def test_two_radio_groups_are_two_tab_stops(ctx) -> None:
    out = _tabs(
        ctx,
        "[{name:'a1',tag:'input',attrs:{type:'radio',name:'ga'}},{name:'a2',tag:'input',attrs:{type:'radio',name:'ga'},checked:true},"
        "{name:'b1',tag:'input',attrs:{type:'radio',name:'gb'}},{name:'b2',tag:'input',attrs:{type:'radio',name:'gb'},checked:true}]",
        "s.a2.focus();",
    )
    assert out["fwd"] is False and out["toFwd"] == "b2", "between the two groups the browser's own Tab stays inside"


@pytest.mark.parametrize(
    "spec",
    [
        "{name:'ed',tag:'div',attrs:{contenteditable:'true'}}",
        "{name:'ed',tag:'div',attrs:{contenteditable:''}}",
        "{name:'dt',tag:'details'},{name:'ed',tag:'summary',within:'dt'}",       # a summary is a stop as the first summary of a details
        "{name:'ed',tag:'iframe'}",
        "{name:'ed',tag:'audio',attrs:{controls:''}}",
        "{name:'ed',tag:'video',attrs:{controls:''}}",
    ],
    ids=["contenteditable", "contenteditable-empty", "summary", "iframe", "audio-controls", "video-controls"],
)
def test_controls_the_browser_tabs_to_but_the_selector_omitted_are_tab_stops(ctx, spec: str) -> None:
    """T5: ``[contenteditable]``, ``summary``, ``iframe`` and ``audio/video[controls]`` were not in the selector. In the toolsets overlay the Python editor (a contenteditable) comes after the last listed control, so a
    Tab from that control wrapped past it and the editor could not be reached with the keyboard."""
    before = _tabs(ctx, f"[{{name:'first',tag:'button'}},{spec}]", "s.first.focus();")
    assert before["fwd"] is False and before["toFwd"] == "ed", f"the Tab skipped the control: {before}"
    at = _tabs(ctx, f"[{{name:'first',tag:'button'}},{spec}]", "s.ed.focus();")
    assert at["fwd"] is True and at["toFwd"] == "first", f"and the Tab from it wraps: {at}"


@pytest.mark.parametrize("spec", ["{name:'x',tag:'div',attrs:{contenteditable:'false'}}", "{name:'x',tag:'audio'}", "{name:'x',tag:'video'}", "{name:'x',tag:'div'}"], ids=["contenteditable-false", "audio", "video", "div"])
def test_things_the_browser_does_not_tab_to_are_not_stops(ctx, spec: str) -> None:
    end = _tabs(ctx, f"[{{name:'first',tag:'button'}},{{name:'last',tag:'button'}},{spec}]", "s.last.focus();")
    assert end["fwd"] is True and end["toFwd"] == "first", end


# ---- round 2 of the #729 review: the tabindex -1 pins for every clause, the checkVisibility options, render order against a re-attach, the radio type, the fallback, and a focus that is lost mid-dialog ----


@pytest.mark.parametrize(
    "tag,attrs",
    [
        ("summary", "{tabindex:'-1'}"),
        ("iframe", "{tabindex:'-1'}"),
        ("audio", "{tabindex:'-1',controls:''}"),
        ("video", "{tabindex:'-1',controls:''}"),
        ("div", "{tabindex:'-1',contenteditable:'true'}"),
    ],
    ids=["summary", "iframe", "audio-controls", "video-controls", "contenteditable"],
)
def test_every_clause_of_the_selector_leaves_out_a_control_with_tabindex_minus_one(ctx, tag: str, attrs: str) -> None:
    """The five clauses T3 did not pin (``summary``, ``iframe``, ``audio[controls]``, ``video[controls]``, ``[contenteditable]``): with tabindex -1 the browser skips them, and at an end such a control became ``last``/``first``."""
    spec = f"{{name:'skipped',tag:'{tag}',attrs:{attrs}}}"
    end = _tabs(ctx, f"[{{name:'first',tag:'button'}},{{name:'last',tag:'button'}},{spec}]", "s.last.focus();")
    assert end["fwd"] is True and end["toFwd"] == "first", end
    start = _tabs(ctx, f"[{spec},{{name:'first',tag:'button'}},{{name:'last',tag:'button'}}]", "s.first.focus();")
    assert start["back"] is True and start["toBack"] == "last", start


def test_the_visibility_options_are_what_hide_a_visibility_hidden_control(ctx) -> None:
    """Chromium's ``checkVisibility()`` with no options reports ``visibility:hidden`` as visible; only ``{ visibilityProperty: true }`` looks at the property. The fake honours that, so a hook that dropped the options
    (mutant M10 of the review) fails this and T2 regresses with every other test green. ``collapse`` is hidden too."""
    for css in ("hidden", "collapse"):
        end = _tabs(ctx, f"[{{name:'first',tag:'button'}},{{name:'last',tag:'button'}},{{name:'ghost',tag:'button',cssVisibility:'{css}'}}]", "s.last.focus();")
        assert end["fwd"] is True and end["toFwd"] == "first", (css, end)


def test_the_fallback_counts_a_fixed_control_and_not_a_collapsed_one(ctx) -> None:
    """Without ``checkVisibility`` (Safari before 17.4) the test was ``offsetParent !== null && visibility !== "hidden"``: a ``position:fixed`` control has no ``offsetParent`` and was dropped (so it was unreachable, as before
    the PR), and ``visibility:collapse`` was counted (so a Tab from the real last stop left the dialog)."""
    fixed = _tabs(ctx, "[{name:'first',tag:'button'},{name:'pinned',tag:'button',cssPosition:'fixed'}]", "s.first.focus();", modern=False)
    assert fixed["fwd"] is False and fixed["toFwd"] == "pinned", f"the Tab skipped a fixed control the browser tabs to: {fixed}"
    pinned_last = _tabs(ctx, "[{name:'first',tag:'button'},{name:'pinned',tag:'button',cssPosition:'fixed'}]", "s.pinned.focus();", modern=False)
    assert pinned_last["fwd"] is True and pinned_last["toFwd"] == "first", pinned_last
    collapsed = _tabs(ctx, "[{name:'first',tag:'button'},{name:'last',tag:'button'},{name:'ghost',tag:'button',cssVisibility:'collapse'}]", "s.last.focus();", modern=False)
    assert collapsed["fwd"] is True and collapsed["toFwd"] == "first", collapsed


def test_a_radio_whose_type_is_spelled_in_capitals_is_grouped_like_any_other(ctx) -> None:
    """``type="RADIO"`` is a radio (the browser normalises it): the group is one stop. ``getAttribute("type") === "radio"`` did not group them, and a Shift+Tab from the real first stop left the dialog."""
    radios = "{name:'r1',tag:'input',attrs:{type:'RADIO',name:'g'}},{name:'r2',tag:'input',attrs:{type:'RADIO',name:'g'},checked:true},{name:'r3',tag:'input',attrs:{type:'RADIO',name:'g'}}"
    start = _tabs(ctx, f"[{radios},{{name:'last',tag:'button'}}]", "s.r2.focus();")
    assert start["back"] is True and start["toBack"] == "last", start
    end = _tabs(ctx, f"[{{name:'first',tag:'button'}},{radios}]", "s.r2.focus();")
    assert end["fwd"] is True and end["toFwd"] == "first", end


def test_the_dialog_that_opened_last_is_the_top_one_whatever_order_the_effects_run_in_and_a_reattach_does_not_move_it(ctx) -> None:
    """Order is taken during RENDER (the opener is), so it follows the order the dialogs became active. Mutant M4 (order taken in the effect) and M5 (order taken again when a dialog re-attaches) both put the lower
    dialog on top; the first survived the V8 file because every test committed in render order."""
    out = json.loads(ctx.eval("""JSON.stringify((function () {
      DOC.length = 0;
      var opener = ne('opener', { tag: 'button' });
      var A = ne('A', { attrs: { tabindex: '-1' } }); A.__node = true; var a1 = ne('a1', { tag: 'button' }, A), a2 = ne('a2', { tag: 'button' }, A);
      var B = ne('B', { attrs: { tabindex: '-1' } }); B.__node = true; var b1 = ne('b1', { tag: 'button' }, B), b2 = ne('b2', { tag: 'button' }, B);
      DOC.push(opener, A, a1, a2, B, b1, b2); settle(); document.activeElement = opener;
      var hA = inst(), hB = inst(), refA = { current: A }, refB = { current: B };
      __h = hA; render(refA, true, null, []);                              // A becomes active first ...
      __h = hB; render(refB, true, null, []);                              // ... B second: B is on top
      __h = hB; commit();                                                    // but B's effect runs BEFORE A's (children first, as React does it)
      __h = hA; commit();
      document.activeElement = document.body; pressAt(false);
      var effect_order = document.activeElement.parent.name;
      __h = hA; render(refA, true, null, ['again']); commit();               // A re-attaches (its dependencies changed): it does not become the newest
      document.activeElement = document.body; pressAt(false);
      var after_reattach = document.activeElement.parent.name;
      return { effect_order: effect_order, after_reattach: after_reattach };
    })())"""))
    assert out == {"effect_order": "B", "after_reattach": "B"}, out


def _lost(ctx, specs: str, focus: str, how: str, shift: bool) -> str:
    """Open a dialog from ``specs``, focus ``s.<focus>`` inside it, lose that element (``remove`` it from the document, or ``disable`` it in place) so the browser hands focus to <body>, press Tab (or Shift+Tab) and say where focus lands."""
    return ctx.eval(f"""(function () {{
      unmount(); __h = inst();
      var s = scene3({specs}), ref = {{ current: s.dialog }};
      render(ref, true, null, []); commit();
      s.{focus}.focus();
      if ('{how}' === 'remove') {{ DOC.splice(DOC.indexOf(s.{focus}), 1); s.{focus}.parent = null; }} else {{ s.{focus}.attrs.disabled = ''; }}
      settle();
      document.activeElement = document.body;
      pressAt({str(shift).lower()});
      return document.activeElement.name;
    }})()""")


_THREE = "[{name:'a',tag:'button'},{name:'b',tag:'button'},{name:'c',tag:'button'}]"


@pytest.mark.parametrize("how", ["remove", "disable"])
def test_a_focus_lost_from_the_middle_of_the_dialog_continues_in_place(ctx, how: str) -> None:
    """Review nit 1: a control that holds focus is disabled (a busy submit) or removed (a menu's search box on Escape) and the browser hands focus to <body>; the document listener restarted at the dialog's FIRST stop
    (or its last on Shift+Tab), where the browser would have gone on from the lost control's place (the Create session bind menu: Tab went to the close button, the next field before the PR)."""
    assert _lost(ctx, _THREE, "b", how, False) == "c"
    assert _lost(ctx, _THREE, "b", how, True) == "a"


@pytest.mark.parametrize("how", ["remove", "disable"])
def test_a_focus_lost_from_an_end_wraps(ctx, how: str) -> None:
    assert _lost(ctx, _THREE, "c", how, False) == "a", "nothing after the lost last stop: the Tab wraps to the first"
    assert _lost(ctx, _THREE, "a", how, True) == "c", "nothing before the lost first stop: the Shift+Tab wraps to the last"
    assert _lost(ctx, _THREE, "c", how, True) == "b"
    assert _lost(ctx, _THREE, "a", how, False) == "b"


def test_a_focus_on_the_page_behind_the_scrim_still_restarts_at_the_ends(ctx) -> None:
    """Nothing to continue from: focus sits on the page behind, not on <body> after a control inside was lost. The restart of round 3 stays."""
    out = _tabs(ctx, _THREE, "s.opener.focus();")
    assert out["toFwd"] == "a" and out["toBack"] == "c", out


def test_a_lost_focus_that_was_not_a_tab_stop_continues_from_its_place(ctx) -> None:
    """A tabindex -1 heading between two stops held focus and was removed: Tab goes to the stop after its place, Shift+Tab to the one before."""
    specs = "[{name:'a',tag:'button'},{name:'h',tag:'h3',attrs:{tabindex:'-1'}},{name:'b',tag:'button'}]"
    assert _lost(ctx, specs, "h", "remove", False) == "b"
    assert _lost(ctx, specs, "h", "remove", True) == "a"


# ---- review nit 2: what the selector still counted although the browser does not tab to it ----

_FIELDSET = "{name:'fs',tag:'fieldset',attrs:{disabled:''}},{name:'x',tag:'input',within:'fs'},{name:'y',tag:'button',within:'fs'}"


def test_the_controls_under_a_disabled_fieldset_are_not_stops(ctx) -> None:
    """``:not([disabled])`` looks at the control's own attribute; a control under ``<fieldset disabled>`` is disabled too (``:disabled``) and the browser skips it. At an end it became ``last`` and a Tab from the real last
    stop left the dialog."""
    end = _tabs(ctx, f"[{{name:'first',tag:'button'}},{{name:'last',tag:'button'}},{_FIELDSET}]", "s.last.focus();")
    assert end["fwd"] is True and end["toFwd"] == "first", end
    start = _tabs(ctx, f"[{_FIELDSET},{{name:'first',tag:'button'}},{{name:'last',tag:'button'}}]", "s.first.focus();")
    assert start["back"] is True and start["toBack"] == "last", start


def test_an_inert_subtree_and_an_inert_control_are_not_stops(ctx) -> None:
    subtree = "{name:'box',tag:'div',attrs:{inert:''}},{name:'z',tag:'button',within:'box'}"
    end = _tabs(ctx, f"[{{name:'first',tag:'button'}},{{name:'last',tag:'button'}},{subtree}]", "s.last.focus();")
    assert end["fwd"] is True and end["toFwd"] == "first", end
    own = _tabs(ctx, "[{name:'first',tag:'button'},{name:'last',tag:'button'},{name:'z',tag:'button',attrs:{inert:''}}]", "s.last.focus();")
    assert own["fwd"] is True and own["toFwd"] == "first", own


def test_an_empty_tabindex_is_no_tabindex(ctx) -> None:
    """``tabindex=""`` is ignored by the browser: a ``div`` with it is no stop (it matched ``[tabindex]``), and a button with it keeps being one."""
    div = _tabs(ctx, "[{name:'first',tag:'button'},{name:'last',tag:'button'},{name:'e',tag:'div',attrs:{tabindex:''}}]", "s.last.focus();")
    assert div["fwd"] is True and div["toFwd"] == "first", div
    button = _tabs(ctx, "[{name:'first',tag:'button'},{name:'b',tag:'button',attrs:{tabindex:''}}]", "s.first.focus();")
    assert button["fwd"] is False and button["toFwd"] == "b", button
    zero = _tabs(ctx, "[{name:'first',tag:'button'},{name:'d',tag:'div',attrs:{tabindex:'0'}}]", "s.first.focus();")
    assert zero["fwd"] is False and zero["toFwd"] == "d", zero


def test_only_the_first_summary_of_a_details_is_a_stop(ctx) -> None:
    two = "{name:'dt',tag:'details'},{name:'s1',tag:'summary',within:'dt'},{name:'s2',tag:'summary',within:'dt'}"
    end = _tabs(ctx, f"[{{name:'first',tag:'button'}},{two}]", "s.s1.focus();")
    assert end["fwd"] is True and end["toFwd"] == "first", f"the second summary was counted as the last stop: {end}"
    lone = _tabs(ctx, "[{name:'first',tag:'button'},{name:'dt',tag:'details'},{name:'s1',tag:'summary',within:'dt'}]", "s.first.focus();")
    assert lone["fwd"] is False and lone["toFwd"] == "s1", lone
