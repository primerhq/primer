"""A plain Escape in the Python code editor is the editor's: it never reaches the console's Escape stack (board task 01a121a5-c13c, found in the review of #700).

CodeMirror 6 enters its tab-focus mode on an Escape keydown (the next Tab leaves the editor instead of indenting; ``python-code-editor.jsx`` keeps ``indentWithTab`` first and counts on it) and does NOT call
``preventDefault`` on that Escape. The console's Escape stack (``ui/foundation/escape-stack.js``) answers an Escape that nothing handled by closing the top layer, so Escape-then-Tab in the editor closed the toolsets
overlay and discarded the unsaved code. The editor's keymap now ends with a plain-Escape entry that returns ``true`` (CodeMirror then calls ``preventDefault``, which the stack ignores) after every entry
that has a use for Escape (the completion list, the search panel, the selection) has had its turn.

A handled keydown never reaches the library's own keydown handler, the one that starts the tab-focus mode, so the entry starts it itself with ``view.setTabFocusMode(2000)`` (the same two seconds the library gives
it); without that call the Tab after an Escape would indent and the keyboard user would be stuck in the editor. The journey found this: the first version of the entry returned ``true`` only.

The REAL ``PY_CodeEditor`` runs in V8 on the mini React against a stand-in for ``window.CM6`` that records the keymap it is handed; the keymap's behaviour in a browser (Escape keeps the overlay, Tab leaves the editor) is
``tests/ui_e2e/test_python_editor_escape_journey.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]

_STAND_IN = """
window.addEventListener = function () {}; window.removeEventListener = function () {};
var __keymap = null;
// the mini React has no DOM, so a ref that React would point at the host element stays null and the editor never mounts: give the first ref (the host) a stand-in element
var __useRef = React.useRef;
React.useRef = function (init) { var r = __useRef(init); if (init === null && r.current === null) r.current = {}; return r; };
function stub(path) {
  var f = function () { return stub(path + "()"); };
  return new Proxy(f, {
    get: function (t, k) {
      if (k === "of") return function (x) { return { of: path, arg: x }; };
      if (typeof k === "symbol") return undefined;
      return stub(path + "." + String(k));
    },
    construct: function () { return { destroy: function () {}, dispatch: function () {}, state: { doc: { toString: function () { return ""; } } } }; },
  });
}
var __own = {
  keymap: { of: function (entries) { __keymap = entries; return { keymap: true }; } },
  indentWithTab: { id: "indentWithTab", key: "Tab" },
  closeBracketsKeymap: [{ id: "closeBrackets", key: "Backspace", run: function () { return false; } }],
  completionKeymap: [{ id: "completion", key: "Escape", run: function () { return false; } }],      // the list is not open: nothing to close
  searchKeymap: [{ id: "search", key: "Escape", run: function () { return false; } }],              // the panel is not open
  historyKeymap: [{ id: "history", key: "Mod-z", run: function () { return false; } }],
  defaultKeymap: [{ id: "default", key: "Escape", run: function () { return false; } }],            // simplifySelection: nothing to simplify
};
window.CM6 = new Proxy(__own, { get: function (t, k) { return k in t ? t[k] : stub(String(k)); } });
"""


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(ROOT / "ui" / "components" / "toolsets" / "python-code-editor.jsx")


@pytest.fixture
def keymap(code):
    ctx = mini_react_context("", _STAND_IN)
    ctx.eval(code)
    ctx.eval("MR.mount(PY_CodeEditor, { value: 'x = 1', onChange: function () {}, diagnostics: [] });")
    try:
        yield ctx
    finally:
        ctx.close()


def _entries(ctx) -> list[dict]:
    return json.loads(ctx.eval("JSON.stringify((__keymap || []).map(function (e) { return { id: e.id || null, key: e.key || null }; }))"))


def test_the_editors_keymap_ends_with_a_plain_escape_that_consumes_it(keymap) -> None:
    entries = _entries(keymap)
    assert entries, "the editor was not mounted with a keymap"
    last = entries[-1]
    assert last["key"] == "Escape" and last["id"] is None, f"the last entry is not the editor's own plain Escape: {entries}"
    keymap.eval("var __view = { calls: [], setTabFocusMode: function (x) { this.calls.push(x); } };")
    assert keymap.eval("__keymap[__keymap.length - 1].run(__view)") is True, "it must return true so that CodeMirror calls preventDefault"


def test_the_consuming_escape_starts_the_tab_focus_mode_the_library_would_have_started(keymap) -> None:
    """A keymap entry that handles the key stops the library's own keydown handler, which is what enters the tab-focus mode; the entry does it, for the library's two seconds, once."""
    keymap.eval("var __view = { calls: [], setTabFocusMode: function (x) { this.calls.push(x); } }; __keymap[__keymap.length - 1].run(__view);")
    assert json.loads(keymap.eval("JSON.stringify(__view.calls)")) == [2000]


def test_every_library_entry_that_uses_escape_runs_before_the_consuming_one(keymap) -> None:
    """The completion list, the search panel and the selection get their Escape first; the consuming entry only takes what nothing used."""
    ids = [e["id"] for e in _entries(keymap)]
    last_library_escape = max(i for i, e in enumerate(_entries(keymap)) if e["id"] in ("completion", "search", "default"))
    assert last_library_escape < len(ids) - 1, ids


def test_tab_still_indents_and_comes_first(keymap) -> None:
    """The consuming Escape does not change Tab: ``indentWithTab`` is still the first entry, so Tab indents until the editor's tab-focus mode (entered by Escape) is on."""
    assert _entries(keymap)[0]["id"] == "indentWithTab"
