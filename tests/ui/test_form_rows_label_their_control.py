"""The Platform form rows tie their label to the control they name (console review C-003, the rest of the console).

``WS_FieldRow`` (workspace providers and templates) and ``FieldRow`` (the semantic-search provider forms) drew ``<label class="field-label">`` as a SIBLING of the input, with no
``htmlFor`` and no ``id``: ``input.labels`` was empty, clicking the visible label focused nothing, and a screen reader landed on an unnamed edit field (72 fields in those
three files). The row now gives its first native control (``input``, ``select``, ``textarea``) an id and points the label at it; a row whose control is a custom component
(or sits inside a wrapper) cannot be labelled by id, so the row becomes a ``role="group"`` named by its label; an error is announced and tied to the control
(``aria-invalid``, ``aria-describedby``, ``role="alert"``).

Both real components run in V8 on the hook runtime in ``tests/ui/_mini_react.py``, with the handful of React APIs they need (``useId``, ``Children``, ``cloneElement``)
stubbed on its ``React`` and ``createElement`` wrapped so the host elements a render produced (their ``id``, ``htmlFor``, ``role``, ``aria-*``) can be read.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
SHARED = (ROOT / "ui" / "components" / "workspaces" / "shared.jsx").read_text(encoding="utf-8")
SEARCH = (ROOT / "ui" / "components" / "semantic-search.jsx").read_text(encoding="utf-8")

_PRELUDE = r"""
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
  var props = Object.assign({}, el.props, extra);
  return { __el: true, type: el.type, props: props, key: el.key, children: el.children, out: null };
};
var ELS = [];
var __ce = React.createElement;
React.createElement = function (type, props) {
  var el = __ce.apply(null, arguments);
  if (typeof type === "string") ELS.push({ type: type, props: props || {} });
  return el;
};
function __view() {
  return JSON.stringify(ELS.map(function (e) {
    var p = e.props;
    return { type: e.type, id: p.id, htmlFor: p.htmlFor, role: p.role, className: p.className, labelledBy: p["aria-labelledby"],
             invalid: p["aria-invalid"], describedBy: p["aria-describedby"] };
  }));
}
"""


def _fn(src: str, name: str) -> str:
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start) + len("\n}\n")]


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    source = "\n".join([
        _fn(SHARED, "WS_fieldControls") if "function WS_fieldControls(" in SHARED else "",
        _fn(SHARED, "WS_FieldRow"), _fn(SEARCH, "FieldRow"),
    ])
    try:
        return bundler._transform(source, "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture(params=["WS_FieldRow", "FieldRow"])
def row(request):
    """``row(children_js, props_js)`` -> the host elements the component drew."""
    made = []

    def go(children_js: str, props: dict | None = None) -> list[dict]:
        ctx = mini_react_context(_compiled(), _PRELUDE + "\nwindow.WS_fieldControls = typeof WS_fieldControls === 'function' ? WS_fieldControls : undefined;")
        made.append(ctx)
        ctx.eval(
            f"function Host() {{ return React.createElement({request.param}, {json.dumps({'label': 'name', **(props or {})})}, {children_js}); }}"
            " MR.mount(Host, {}); ELS.length = 0; MR.rerender();"
        )
        return [{k: e.get(k) for k in ("type", "id", "htmlFor", "role", "className", "labelledBy", "invalid", "describedBy")} for e in json.loads(ctx.eval("__view()"))]

    try:
        yield go
    finally:
        for c in made:
            c.close()


def _one(view: list[dict], type_: str) -> dict:
    found = [e for e in view if e["type"] == type_]
    assert len(found) == 1, (type_, view)
    return found[0]


@pytest.mark.parametrize("control", ["input", "select", "textarea"])
def test_the_label_points_at_the_native_control_it_names(row, control: str) -> None:
    view = row(f"React.createElement('{control}', {{ className: 'input' }})")
    label, ctrl = _one(view, "label"), _one(view, control)
    assert ctrl["id"] and label["htmlFor"] == ctrl["id"], view


def test_a_control_that_has_its_own_id_keeps_it_and_is_the_labels_target(row) -> None:
    view = row("React.createElement('input', { id: 'my-id' })")
    assert _one(view, "input")["id"] == "my-id" and _one(view, "label")["htmlFor"] == "my-id"


def test_only_the_first_control_of_a_row_is_the_labels_target(row) -> None:
    view = row("React.createElement('input', { className: 'a' }), React.createElement('input', { className: 'b' })")
    inputs = [e for e in view if e["type"] == "input"]
    assert inputs[0]["id"] and not inputs[1]["id"]


def test_a_row_whose_control_is_not_native_becomes_a_group_named_by_its_label(row) -> None:
    view = row("React.createElement('div', { className: 'custom-picker' })")
    label = _one(view, "label")
    assert not label["htmlFor"] and label["id"]
    group = [e for e in view if e["role"] == "group"]
    assert len(group) == 1 and group[0]["labelledBy"] == label["id"], view


def test_a_row_with_a_native_control_is_not_also_a_group(row) -> None:
    view = row("React.createElement('input', {})")
    assert [e for e in view if e["role"] == "group"] == []


def test_an_error_is_announced_and_tied_to_the_control(row) -> None:
    view = row("React.createElement('input', {})", {"err": "required"})
    ctrl = _one(view, "input")
    err = [e for e in view if "field-help" in (e["className"] or "")]
    assert len(err) == 1 and err[0]["role"] == "alert" and err[0]["id"], view
    assert ctrl["invalid"] in ("true", True) and ctrl["describedBy"] == err[0]["id"]


def test_a_row_without_an_error_points_at_nothing(row) -> None:
    view = row("React.createElement('input', {})")
    ctrl = _one(view, "input")
    assert ctrl["invalid"] in (None, "false", False) and ctrl["describedBy"] is None


def test_a_described_by_the_control_already_has_is_kept(row) -> None:
    view = row("React.createElement('input', { 'aria-describedby': 'hint-1' })", {"err": "bad"})
    err = [e for e in view if "field-help" in (e["className"] or "")][0]
    assert _one(view, "input")["describedBy"] == f"hint-1 {err['id']}"


def test_two_rows_never_share_an_id(row) -> None:
    first = _one(row("React.createElement('input', {})"), "input")["id"]
    second = _one(row("React.createElement('input', {})"), "input")["id"]
    assert first and second and first != second
