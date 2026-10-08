"""The sign-in, registration and forced password-change forms are usable with a screen reader (console review C-003, 2026-10-08).

``_PasswordField`` drew ``<label>{label}</label>`` with no ``htmlFor`` and an ``<input>`` with no ``id``, so ``input.labels`` was empty,
clicking the visible label focused nothing, and a screen-reader user landed on an unnamed edit field. Its reveal button was named just "Show
password" on every field of a form that has two or three of them, and a failed submit was silent: the field errors and the server banner had no
live-region role.

The real ``RegisterScreen``, ``LoginScreen`` and ``ADM_MustChangePasswordScreen`` run in V8 on the hook runtime in ``tests/ui/_mini_react.py``;
``React.createElement`` is wrapped so the host elements each render produced (their ``id``, ``htmlFor``, ``role`` and ``aria-*``) can be read,
because the stand-in has no DOM to ask for ``input.labels``. A label counts as naming an input here only when its ``htmlFor`` equals the input's
``id`` and the ``id`` is unique on the screen, which is what the browser needs to associate them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "ui" / "components" / "auth.jsx"

_PRELUDE = r"""
window.location = { search: "", pathname: "/console/", hash: "" };
window.history = { state: null, replaceState: function () {} };
window.primerApi = { apiFetch: function () { return window.__next(); } };
window.__next = function () { return Promise.resolve({ providers: [] }); };

// Every host element a render produced, with its props and the text of its string children.
var ELS = [];
var __ce = React.createElement;
React.createElement = function (type, props) {
  var el = __ce.apply(null, arguments);
  if (typeof type === "string") {
    var text = Array.prototype.slice.call(arguments, 2).filter(function (c) { return typeof c === "string"; }).join("");
    ELS.push({ type: type, props: props || {}, text: text });
  }
  return el;
};
function __view() {
  return JSON.stringify(ELS.map(function (e) {
    var p = e.props;
    return {
      type: e.type, inputType: p.type, id: p.id, htmlFor: p.htmlFor, role: p.role, className: p.className, text: e.text,
      ariaLabel: p["aria-label"], ariaInvalid: p["aria-invalid"], describedBy: p["aria-describedby"], ariaLive: p["aria-live"],
    };
  }));
}
function __draw() { ELS.length = 0; MR.rerender(); }
function __first(pred) { return ELS.filter(pred)[0]; }
"""


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(AUTH)


def _screen(code: str, name: str, props: str = "{ onDone: function () {} }"):
    ctx = mini_react_context(code, _PRELUDE)
    ctx.eval(f"MR.mount({name}, {props}); __draw();")
    return ctx


@pytest.fixture(params=["RegisterScreen", "LoginScreen", "ADM_MustChangePasswordScreen"])
def screen(request, code):
    ctx = _screen(code, request.param)
    try:
        yield request.param, ctx
    finally:
        ctx.close()


_FIELDS = ("type", "inputType", "id", "htmlFor", "role", "className", "text", "ariaLabel", "ariaInvalid", "describedBy", "ariaLive")


def _view(ctx) -> list[dict]:
    """JSON.stringify drops an undefined prop, so every field is defaulted to None here."""
    return [{k: e.get(k) for k in _FIELDS} for e in json.loads(ctx.eval("__view()"))]


def _typed(ctx, label_text: str, value: str) -> None:
    """Type into the input that the label with this text names, through the input's own onChange."""
    ctx.eval(
        "(function () {"
        f"var lab = __first(function (e) {{ return e.type === 'label' && e.text === {json.dumps(label_text)}; }});"
        "var inp = __first(function (e) { return e.type === 'input' && e.props.id === lab.props.htmlFor; });"
        f"inp.props.onChange({{ target: {{ value: {json.dumps(value)} }} }});"
        "})(); __draw();"
    )


def _submit(ctx) -> None:
    ctx.eval("__first(function (e) { return e.type === 'form'; }).props.onSubmit({ preventDefault: function () {} }); __draw();")


def test_every_text_and_password_input_is_named_by_a_label_that_points_at_its_id(screen) -> None:
    name, ctx = screen
    view = _view(ctx)
    inputs = [e for e in view if e["type"] == "input" and e["inputType"] != "checkbox"]
    assert inputs, f"{name} drew no inputs"
    ids = [i["id"] for i in inputs]
    assert all(ids), f"{name}: an input without an id has no label to point at it: {inputs}"
    assert len(set(ids)) == len(ids), f"{name}: ids must be unique on a screen: {ids}"
    labels = {e["htmlFor"]: e["text"] for e in view if e["type"] == "label" and e["htmlFor"]}
    for i in inputs:
        assert labels.get(i["id"]), f"{name}: no label has htmlFor={i['id']!r}"


def test_the_password_labels_keep_their_visible_words(screen) -> None:
    name, ctx = screen
    texts = [e["text"] for e in _view(ctx) if e["type"] == "label" and e["htmlFor"]]
    expected = {
        "RegisterScreen": ["Username", "Password", "Confirm password"],
        "LoginScreen": ["Username", "Password"],
        "ADM_MustChangePasswordScreen": ["Current password", "New password", "Confirm new password"],
    }[name]
    assert texts == expected


def test_each_reveal_button_names_the_field_it_reveals(screen) -> None:
    name, ctx = screen
    names = [e["ariaLabel"] for e in _view(ctx) if e["type"] == "button" and (e["ariaLabel"] or "").startswith("Show")]
    expected = {
        "RegisterScreen": ["Show password", "Show confirmation"],
        "LoginScreen": ["Show password"],
        "ADM_MustChangePasswordScreen": ["Show current password", "Show new password", "Show confirmation"],
    }[name]
    assert names == expected
    assert len(set(names)) == len(names), "two buttons with one name are indistinguishable to a screen reader"


def test_a_revealed_field_offers_to_hide_that_same_field(code) -> None:
    ctx = _screen(code, "RegisterScreen")
    try:
        ctx.eval("__first(function (e) { return e.props['aria-label'] === 'Show confirmation'; }).props.onClick(); __draw();")
        names = [e["ariaLabel"] for e in _view(ctx) if e["type"] == "button" and e["ariaLabel"]]
        assert names == ["Show password", "Hide confirmation"]
    finally:
        ctx.close()


def test_a_field_error_is_announced_and_tied_to_its_input(code) -> None:
    ctx = _screen(code, "RegisterScreen")
    try:
        _typed(ctx, "Username", "reviewer")
        _typed(ctx, "Password", "short")
        _typed(ctx, "Confirm password", "different")
        _submit(ctx)
        view = _view(ctx)
        errs = [e for e in view if "field-err" in (e["className"] or "")]
        assert [e["text"] for e in errs] != [], "the submit should have produced field errors"
        for err in errs:
            assert err["role"] == "alert", f"a silent error: {err}"
            assert err["id"], "the input needs something to point aria-describedby at"
            owner = [i for i in view if i["type"] == "input" and i["describedBy"] == err["id"]]
            assert len(owner) == 1, f"exactly one input is described by {err['id']}: {owner}"
            assert owner[0]["ariaInvalid"] in ("true", True)
        valid = [i for i in view if i["type"] == "input" and i["describedBy"] is None and i["inputType"] != "checkbox"]
        assert all(i["ariaInvalid"] in (None, "false", False) for i in valid), "an input without an error is not invalid"
    finally:
        ctx.close()


def test_an_input_without_an_error_points_at_nothing(code) -> None:
    ctx = _screen(code, "LoginScreen")
    try:
        for e in _view(ctx):
            if e["type"] == "input":
                assert e["describedBy"] is None and e["ariaInvalid"] in (None, "false", False), e
    finally:
        ctx.close()


def test_the_server_banner_is_announced(code) -> None:
    ctx = _screen(code, "LoginScreen")
    try:
        ctx.eval("window.__next = function () { return Promise.reject({ status: 401 }); };")
        _typed(ctx, "Username", "reviewer")
        _typed(ctx, "Password", "wrong-password")
        _submit(ctx)
        ctx.eval("__draw();")
        banner = [e for e in _view(ctx) if "auth-banner" in (e["className"] or "")]
        assert len(banner) == 1, "a rejected sign-in should show the banner"
        assert banner[0]["role"] == "alert"
    finally:
        ctx.close()
