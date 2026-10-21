"""The console overlay stays open when a drag-select ends on its scrim.

A browser click uses the nearest common ancestor of the pointer press and
release.  When a drag starts inside the overlay panel and ends on the scrim,
the click therefore targets the scrim even though the press did not.  The
console overlay must distinguish that gesture from a plain scrim click so it
does not discard the open surface and its draft.

The real ``NV_OverlayPanel`` runs in V8 on the mini React runtime.  Only the
focus and Escape hooks, which are outside this interaction, are stubbed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
OVERLAYS = ROOT / "ui" / "components" / "console" / "nv-overlays.jsx"

_PRELUDE = """
window.primerApi = {
  useFocusTrap: function () {},
  focusablesOf: function () { return []; },
  useEscape: function () {},
};
"""

_DRIVER = r"""
var __closed = 0;
var __scrim, __panel;
function __mount() {
  __closed = 0;
  MR.mount(function () {
    return React.createElement(NV_OverlayPanel, {
      title: "Edit agent",
      testid: "panel",
      onClose: function () { __closed += 1; }
    }, "draft text");
  }, {});
  __scrim = MR.findProp("className", "nv-scrim");
  __panel = MR.find("panel");
}
function __evt(target) {
  return {
    target: target,
    currentTarget: __scrim,
    preventDefault: function () {},
    stopPropagation: function () {}
  };
}
function __pressThenClick(pressTarget) {
  if (__scrim.props.onPointerDown) {
    __scrim.props.onPointerDown(__evt(pressTarget));
  }
  __scrim.props.onClick(__evt(__scrim));
}
function __closedNow() { return __closed; }
"""


@pytest.fixture(scope="module")
def ctx():
    context = mini_react_context(transpile(OVERLAYS), _PRELUDE)
    context.eval(_DRIVER)
    yield context
    context.close()


def _closed(ctx) -> int:
    return ctx.eval("__closedNow()")


def test_a_drag_select_that_starts_inside_and_ends_on_the_scrim_keeps_the_overlay_open(ctx) -> None:
    ctx.eval("__mount(); __pressThenClick(__panel);")
    assert _closed(ctx) == 0


def test_a_plain_click_on_the_scrim_still_closes_the_overlay(ctx) -> None:
    ctx.eval("__mount(); __pressThenClick(__scrim);")
    assert _closed(ctx) == 1


def test_a_click_inside_the_panel_does_not_close_the_overlay(ctx) -> None:
    ctx.eval("__mount(); __panel.props.onClick(__evt(__panel));")
    assert _closed(ctx) == 0
