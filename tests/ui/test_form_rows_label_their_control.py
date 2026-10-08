"""The labelled form row ties its label to the control it names (console review C-003), and there is ONE row implementation.

``FormField`` (``ui/components/shared/form-field.jsx``) is the row. ``WS_FieldRow`` (workspace providers and templates) and ``FieldRow`` (the semantic-search provider forms) used to be
two copies of it and are now one-line wrappers, so a form row anywhere in the console is labelled the same way. The row used to draw ``<label class="field-label">`` as a SIBLING of the input, with no
``htmlFor`` and no ``id``: ``input.labels`` was empty, clicking the visible label focused nothing, and a screen reader landed on an unnamed edit field. The row now gives its first native
control (``input``, ``select``, ``textarea``) an id and points the label at it; a row whose control is a custom component (or sits inside a wrapper) cannot be labelled by id, so the row
becomes a ``role="group"`` named by its label; an error is announced and tied to the control (``aria-invalid``, ``aria-describedby``, ``role="alert"``).

All three components run in V8 on the hook runtime in ``tests/ui/_mini_react.py``, with the handful of React APIs they need (``useId``, ``Children``, ``cloneElement``)
stubbed on its ``React`` and ``createElement`` wrapped so the host elements a render produced (their ``id``, ``htmlFor``, ``role``, ``aria-*``) can be read.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
FORM_PATH = ROOT / "ui" / "components" / "shared" / "form-field.jsx"
SHARED = (ROOT / "ui" / "components" / "workspaces" / "shared.jsx").read_text(encoding="utf-8")
SEARCH = (ROOT / "ui" / "components" / "semantic-search.jsx").read_text(encoding="utf-8")
INDEX = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")

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
var ELS = [];
React.cloneElement = function (el, extra) {
  var props = Object.assign({}, el.props, extra);
  // the clone REPLACES the element it was made from in what the test reads (the original is what the caller created, before the row wired it)
  for (var i = 0; i < ELS.length; i++) if (ELS[i].props === el.props) { ELS[i] = { type: el.type, props: props }; break; }
  return { __el: true, type: el.type, props: props, key: el.key, children: el.children, out: null };
};
var __ce = React.createElement;
React.createElement = function (type, props) {
  var el = __ce.apply(null, arguments);
  if (typeof type === "string") ELS.push({ type: type, props: el.props });   // the element's own props object: a clone is matched to its original by it
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
    form = FORM_PATH.read_text(encoding="utf-8")
    source = "\n".join([_fn(form, "FF_fieldControls"), _fn(form, "FormField"), _fn(SHARED, "WS_FieldRow"), _fn(SEARCH, "FieldRow")])
    try:
        return bundler._transform(source, "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture(params=["FormField", "WS_FieldRow", "FieldRow"])
def row(request):
    """``row(children_js, props_js)`` -> the host elements the component drew."""
    made = []

    def go(children_js: str, props: dict | None = None, copies: int = 1) -> list[dict]:
        ctx = mini_react_context(_compiled(), _PRELUDE)
        made.append(ctx)
        one = f"React.createElement({request.param}, {json.dumps({'label': 'name', **(props or {})})}, {children_js})"
        ctx.eval(
            f"function Host() {{ return React.createElement(React.Fragment, null, {', '.join([one] * copies)}); }}"
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
    inputs = [e for e in row("React.createElement('input', {})", copies=2) if e["type"] == "input"]
    labels = [e for e in row("React.createElement('input', {})", copies=2) if e["type"] == "label"]
    assert len(inputs) == 2 and inputs[0]["id"] and inputs[1]["id"] and inputs[0]["id"] != inputs[1]["id"]
    assert [lab["htmlFor"] for lab in labels] == [i["id"] for i in inputs]


# ---- one implementation ----------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["WS_FieldRow", "FieldRow"])
def test_the_older_row_names_are_one_line_wrappers_of_form_field(name: str) -> None:
    body = _fn({"WS_FieldRow": SHARED, "FieldRow": SEARCH}[name], name)
    assert re.search(r"<FormField\s+\{\.\.\.props\}\s*/>", body), body
    assert "<label" not in body and "useId" not in body and "cloneElement" not in body, "a second copy of the row"


def test_the_labelled_row_is_drawn_in_one_file() -> None:
    """Only ``form-field.jsx`` pairs a label with its control by id: a second ``useId`` + ``htmlFor`` row anywhere would be a copy to keep in step."""
    drawn = []
    for path in sorted((ROOT / "ui").rglob("*.jsx")):
        src = path.read_text(encoding="utf-8")
        if "FF_fieldControls(" in src:
            drawn.append(path.relative_to(ROOT / "ui").as_posix())
    assert drawn == ["components/shared/form-field.jsx"], drawn


def test_form_field_is_registered_before_the_pages_that_use_it() -> None:
    """The bundle is built from ``index.html``'s script tags in order: an unregistered file is a ``ReferenceError`` at the first form that renders."""
    tag = 'src="components/shared/form-field.jsx"'
    assert INDEX.count(tag) == 1, "form-field.jsx must be registered exactly once"
    for later in ("components/workspaces/shared.jsx", "components/semantic-search.jsx"):
        assert INDEX.index(tag) < INDEX.index(f'src="{later}"'), f"form-field.jsx must load before {later}"
    assert "window.FormField = FormField" in FORM_PATH.read_text(encoding="utf-8"), "pages built outside the bundle's scope reach it as window.FormField"
