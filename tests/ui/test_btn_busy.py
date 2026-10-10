"""``Btn busy`` keeps a button focusable while its request is out (board task 01a12480-2176).

A button that turns natively ``disabled`` while it has focus drops the focus to ``<body>``: inside a dialog the trap no longer sees Tab, and a keyboard user loses the place they pressed the key in. The
console had ~73 of them (``disabled={busy}`` on a Save, a Delete, a Create). ``Btn`` takes a ``busy`` prop for that: the button stays in the tab order with ``aria-disabled="true"`` and
``aria-busy="true"``, and its click does nothing (and is not allowed to submit a form it sits in). ``disabled`` keeps meaning "this cannot be pressed now, whatever the answer".

The REAL ``Btn`` of ``ui/components/shared.jsx`` runs in V8 on the mini React; only the shell's globals it does not need here are absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "ui" / "components" / "shared.jsx"

_PRELUDE = "window.primerApi = {};"

_DRIVER = """
var __clicks = 0; var __prevented = 0;
function __button(props) {
  MR.mount(function () { return React.createElement(Btn, Object.assign({ "data-testid": "b", onClick: function () { __clicks += 1; } }, props), "Save"); }, {});
  return MR.findAll("b").filter(function (el) { return el.type === "button"; })[0];
}
function __attrs(el) {
  var p = el.props;
  return { disabled: p.disabled === undefined ? null : p.disabled, busy: p["aria-busy"] === undefined ? null : p["aria-busy"], ariaDisabled: p["aria-disabled"] === undefined ? null : p["aria-disabled"], leaked: "busy" in p };
}
function __press(el) {
  if (typeof el.props.onClick === "function") el.props.onClick({ preventDefault: function () { __prevented += 1; }, stopPropagation: function () {} });
  return { clicks: __clicks, prevented: __prevented };
}
"""


@pytest.fixture(scope="module")
def ctx():
    context = mini_react_context(transpile(SHARED), _PRELUDE)
    context.eval(_DRIVER)
    yield context
    context.close()


def _attrs(ctx, props: dict) -> dict:
    return json.loads(ctx.eval(f"JSON.stringify(__attrs(__button({json.dumps(props)})))"))


def test_a_busy_button_stays_enabled_and_says_it_is_busy(ctx) -> None:
    """Not ``disabled`` (that is the whole point: the focus stays), ``aria-disabled`` and ``aria-busy`` instead, and ``busy`` is not passed on to the DOM element as an attribute of its own."""
    assert _attrs(ctx, {"busy": True}) == {"disabled": None, "busy": "true", "ariaDisabled": "true", "leaked": False}


def test_a_button_that_is_not_busy_carries_neither_attribute(ctx) -> None:
    assert _attrs(ctx, {"busy": False}) == {"disabled": None, "busy": None, "ariaDisabled": None, "leaked": False}
    assert _attrs(ctx, {}) == {"disabled": None, "busy": None, "ariaDisabled": None, "leaked": False}


def test_disabled_still_means_disabled(ctx) -> None:
    assert _attrs(ctx, {"disabled": True})["disabled"] is True
    assert _attrs(ctx, {"disabled": True, "busy": False})["disabled"] is True


def test_a_busy_click_does_nothing(ctx) -> None:
    """A second click (or Enter, which is a click) while the request is out must not start another one: the native ``disabled`` used to refuse it, ``aria-disabled`` does not."""
    got = json.loads(ctx.eval('JSON.stringify((function () { var b = __button({ busy: true }); __clicks = 0; return __press(b); })())'))
    assert got["clicks"] == 0


def test_a_busy_click_does_not_submit_the_form_the_button_sits_in(ctx) -> None:
    """``type="submit"`` clicks submit a form unless the click's default is prevented: a busy one prevents it."""
    got = json.loads(ctx.eval('JSON.stringify((function () { var b = __button({ busy: true, type: "submit" }); __clicks = 0; __prevented = 0; return __press(b); })())'))
    assert got == {"clicks": 0, "prevented": 1}


def test_a_button_that_is_not_busy_clicks_through(ctx) -> None:
    got = json.loads(ctx.eval('JSON.stringify((function () { var b = __button({ busy: false }); __clicks = 0; __prevented = 0; return __press(b); })())'))
    assert got == {"clicks": 1, "prevented": 0}


def test_busy_wins_over_an_aria_disabled_the_caller_passed(ctx) -> None:
    """A caller that spells ``aria-disabled="false"`` by hand and also says ``busy`` gets a busy button."""
    got = _attrs(ctx, {"busy": True, "aria-disabled": "false", "aria-busy": "false"})
    assert got["ariaDisabled"] == "true" and got["busy"] == "true"


def test_a_callers_own_aria_attributes_survive_when_the_button_is_not_busy(ctx) -> None:
    """The existing hand-written sites (provider form, the builder's remove) pass ``aria-disabled`` themselves: ``busy`` must not clear them."""
    got = _attrs(ctx, {"aria-disabled": "true", "aria-busy": "true"})
    assert got["ariaDisabled"] == "true" and got["busy"] == "true"
