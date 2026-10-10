"""The Python code editor has an accessible name and tells a keyboard user how to leave it (board task 01a125ec-4e03, found in the review of #728).

The CodeMirror content is a ``contenteditable`` with no name (WCAG 4.1.2), and a keyboard user is told nothing about how to leave an editor that indents on Tab (WCAG 2.1.2): Escape, then Tab within two seconds (the
editor starts the tab-focus mode itself); a SECOND Escape when the first one only collapsed a selection or closed the completion list or the search panel (those entries use Escape first); or Ctrl+M, the
library's own toggle. The content gets ``aria-label`` and ``aria-describedby`` through ``EditorView.contentAttributes``, and the hint is a visually hidden element the description points at. The textarea fallback
(no bundle) is named too.

The REAL ``PY_CodeEditor`` runs in V8 on the mini React against a stand-in for ``window.CM6`` that records the extensions it is handed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]

_STAND_IN = """
window.addEventListener = function () {}; window.removeEventListener = function () {};
var __extensions = null;
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
  keymap: { of: function () { return { keymap: true }; } },
  indentWithTab: {}, closeBracketsKeymap: [], completionKeymap: [], searchKeymap: [], historyKeymap: [], defaultKeymap: [],
  EditorState: { create: function (cfg) { __extensions = cfg.extensions; return {}; } },
};
window.CM6 = new Proxy(__own, { get: function (t, k) { return k in t ? t[k] : stub(String(k)); } });
"""

_PRELUDE_NO_BUNDLE = "window.addEventListener = function () {}; window.removeEventListener = function () {};"


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(ROOT / "ui" / "components" / "toolsets" / "python-code-editor.jsx")


@pytest.fixture
def editor(code):
    ctx = mini_react_context("", _STAND_IN)
    ctx.eval(code)
    ctx.eval("MR.mount(PY_CodeEditor, { value: 'x = 1', onChange: function () {}, diagnostics: [] });")
    try:
        yield ctx
    finally:
        ctx.close()


def _content_attributes(ctx) -> dict:
    found = json.loads(ctx.eval("JSON.stringify((__extensions || []).filter(function (e) { return e && e.of === 'EditorView.contentAttributes'; }).map(function (e) { return e.arg; }))"))
    assert len(found) == 1, f"the editor must set its content attributes once, got {found}"
    return found[0]


def test_the_editable_content_has_an_accessible_name(editor) -> None:
    attrs = _content_attributes(editor)
    assert "python" in attrs.get("aria-label", "").lower(), attrs


def test_the_content_is_described_by_a_hint_that_is_in_the_page(editor) -> None:
    attrs = _content_attributes(editor)
    hint_id = attrs.get("aria-describedby")
    assert hint_id, attrs
    hint = json.loads(editor.eval("JSON.stringify(MR.find('python-source-hint') && MR.find('python-source-hint').props)"))
    assert hint and hint["id"] == hint_id, "aria-describedby points at an element that is not rendered"


def test_the_hint_says_how_to_leave_the_editor(editor) -> None:
    text = editor.eval("MR.texts().join(' ')")
    lowered = text.lower()
    assert "escape" in lowered and "tab" in lowered, text
    assert "two seconds" in lowered or "2 seconds" in lowered, "Escape then Tab only works for two seconds"
    assert "again" in lowered or "second escape" in lowered, "a first Escape that closed the completion list or the search panel needs a second one"
    assert "ctrl" in lowered and "m" in lowered, "the Ctrl+M toggle"


def test_the_hint_is_visually_hidden_and_not_a_tab_stop(editor) -> None:
    hint = json.loads(editor.eval("JSON.stringify(MR.find('python-source-hint').props)"))
    assert "nv-sr-only" in hint.get("className", ""), hint
    assert hint.get("tabIndex") in (None, -1), hint


def test_the_textarea_fallback_is_named_too(code) -> None:
    ctx = mini_react_context("", _PRELUDE_NO_BUNDLE)
    ctx.eval(code)
    ctx.eval("MR.mount(PY_CodeEditor, { value: 'x = 1', onChange: function () {}, diagnostics: [] });")
    try:
        props = json.loads(ctx.eval("JSON.stringify(MR.find('python-source').props)"))
        assert props["data-editor"] == "fallback" and "python" in props.get("aria-label", "").lower(), props
    finally:
        ctx.close()
