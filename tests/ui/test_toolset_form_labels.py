"""The toolset form draws every row as a ``FormField``, and each key/value pair input is named (console review C-003, the toolsets surface).

``TS_NewToolsetModal`` drew ``ID``, ``Provider``, ``Transport``, ``Command`` and ``URL`` as ``<div className="field"><label className="field-label">...`` (the label a SIBLING of the control, the
server's field error a loose ``<div className="field-help">``), and ``TS_KvEditor`` (the environment and headers editors) did the same for a LIST of key/value input pairs whose only names were
their placeholders. Each row is a ``FormField`` now: the label points at the control, the server's message is the row's ``err``, and a row with no native control (the transport chips, the
pairs) is a group named by its label with every pair input named ("Environment key 1", "Environment value 1").
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "toolsets.jsx").read_text(encoding="utf-8")
FORM = ROOT / "ui" / "components" / "shared" / "form-field.jsx"


def _function(src: str, name: str) -> str:
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start) + len("\n}\n")]


def test_no_bare_field_label_is_left() -> None:
    assert '<label className="field-label">' not in SRC


def test_the_form_draws_six_rows_as_form_fields() -> None:
    """ID, Provider, Transport, Command, URL, and the key/value editor's own row."""
    assert SRC.count("<FormField ") == 6
    for label in ('label="ID"', 'label="Provider"', 'label="Transport"', 'label="Command"', 'label="URL"', "label={label} hint={hint}"):
        assert label in SRC, label


def test_the_server_errors_are_each_rows_err_and_not_a_loose_div() -> None:
    assert not re.search(r'fieldErrors\["body\.[a-z_.]+"\] && <div className="field-help"', SRC)
    for key in ("body.id", "body.provider", "body.config.config.command", "body.config.config.url"):
        assert f'err={{fieldErrors["{key}"]}}' in SRC, key


def test_the_locked_hint_is_written_without_a_literal_dash() -> None:
    assert 'hint={isEdit ? "locked \\u2014 id cannot change after create" : "optional \\u2014 backend assigns if blank"}' in SRC


def test_the_page_declares_the_row_it_uses() -> None:
    assert re.match(r"/\* global [^*]*\bFormField\b", SRC)


# ---- the key/value editor, rendered --------------------------------------------------------------------------------------------------------------

_PRELUDE = r"""
function Btn(props) { return React.createElement("button", { title: props.title }, props.children); }
function Icon() { return null; }
var __ids = 0;
React.useId = function () { return ":r" + (__ids++) + ":"; };
React.isValidElement = function (e) { return !!(e && e.__el); };
React.Children = { toArray: function (c) {
  var out = [];
  (function walk(n) { if (n == null || typeof n === "boolean") return; if (Array.isArray(n)) { n.forEach(walk); return; } out.push(n); })(c);
  return out;
} };
React.cloneElement = function (el, extra) {
  return { __el: true, type: el.type, props: Object.assign({}, el.props, extra), key: el.key, children: el.children, out: null };
};
var ELS = [];
var __ce = React.createElement;
React.createElement = function (type, props) {
  var el = __ce.apply(null, arguments);
  if (typeof type === "string") ELS.push({ type: type, props: el.props });
  return el;
};
function __view() {
  return JSON.stringify(ELS.map(function (e) {
    var p = e.props;
    return { type: e.type, id: p.id, htmlFor: p.htmlFor, role: p.role, labelledBy: p["aria-labelledby"], ariaLabel: p["aria-label"], placeholder: p.placeholder };
  }));
}
"""


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    form = FORM.read_text(encoding="utf-8")
    source = "\n".join([_function(form, "FF_fieldControls"), _function(form, "FormField"), _function(SRC, "TS_KvEditor")])
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform(source, "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture
def editor():
    made = []

    def go(pairs: list[dict], label: str = "Environment") -> list[dict]:
        ctx = mini_react_context(_compiled(), _PRELUDE)
        made.append(ctx)
        props = {"label": label, "hint": "optional", "pairs": pairs, "keyPlaceholder": "KEY", "valuePlaceholder": "value"}
        ctx.eval(f"function Host() {{ return React.createElement(TS_KvEditor, {json.dumps(props)}); }} MR.mount(Host, {{}}); ELS.length = 0; MR.rerender();")
        return json.loads(ctx.eval("__view()"))

    try:
        yield go
    finally:
        for c in made:
            c.close()


def test_the_editor_row_is_a_group_named_by_its_label(editor) -> None:
    view = editor([{"key": "A", "value": "1"}])
    label = next(e for e in view if e["type"] == "label")
    group = next(e for e in view if e.get("role") == "group")
    assert not label.get("htmlFor") and group["labelledBy"] == label["id"], view


def test_every_pair_input_has_a_name_of_its_own(editor) -> None:
    view = editor([{"key": "A", "value": "1"}, {"key": "B", "value": "2"}])
    assert [e.get("ariaLabel") for e in view if e["type"] == "input"] == ["Environment key 1", "Environment value 1", "Environment key 2", "Environment value 2"]


def test_the_names_follow_the_editors_label(editor) -> None:
    view = editor([{"key": "Authorization", "value": "Bearer x"}], label="Headers")
    assert [e.get("ariaLabel") for e in view if e["type"] == "input"] == ["Headers key 1", "Headers value 1"]


def test_an_empty_editor_has_no_inputs_and_still_names_its_group(editor) -> None:
    view = editor([])
    assert [e for e in view if e["type"] == "input"] == []
    assert len([e for e in view if e.get("role") == "group"]) == 1
