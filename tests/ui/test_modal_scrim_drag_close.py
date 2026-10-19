"""A drag-select that starts inside a Modal and ends on the scrim must not close it (ticket 01a14f37, found in the #779 review).

The scrim (the desktop's ``.modal-overlay``, the phone's ``.sheet-overlay``) closed on any click that reached it. A
click's target is the nearest common ancestor of where the press and the release landed, so a drag-select that starts
inside the dialog (selecting text in the agent overlay's big prompt editor, dragging a textarea resize handle) and ends
on the scrim clicked the scrim and closed the dialog, losing the draft. The scrim now records where the pointerdown
landed and closes only when both the press and the click landed on the scrim itself: a plain click on the scrim still
closes, and the Escape path (foundation/escape-stack.js) is untouched.

The REAL ``Modal`` of ``ui/components/shared.jsx`` runs in V8 on the mini React; only the shell globals it does not
need here are stubbed in the prelude.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "ui" / "components" / "shared.jsx"

_PRELUDE = """
var document = { body: { style: {} } };
window.primerApi = {
  useEscape: function () {},
  useFocusTrap: function () {},
  useViewport: null,
};
"""

_DRIVER = r"""
var __closed = 0;
var __scrim, __dialog;
function __mount(mobile) {
  window.primerApi.useViewport = mobile ? function () { return { isMobile: true }; } : null;
  __closed = 0;
  MR.mount(function () {
    return React.createElement(Modal, {
      title: "Edit agent",
      testid: "m1",
      onClose: function () { __closed += 1; }
    }, "draft text");
  }, {});
  __scrim = MR.findProp("className", "modal-overlay") || MR.findProp("className", "sheet-overlay");
  __dialog = MR.find("m1");
}
function __evt(target) {
  return { target: target, currentTarget: __scrim, preventDefault: function () {}, stopPropagation: function () {} };
}
function __pressThenClick(pressTarget) {
  __scrim.props.onPointerDown(__evt(pressTarget));
  __scrim.props.onClick(__evt(__scrim));
}
function __closedNow() { return __closed; }
"""


@pytest.fixture(scope="module")
def ctx():
    context = mini_react_context(transpile(SHARED), _PRELUDE)
    context.eval(_DRIVER)
    yield context
    context.close()


def _closed(ctx) -> int:
    return ctx.eval("__closedNow()")


def test_a_drag_select_that_starts_inside_and_ends_on_the_scrim_keeps_the_modal_open(ctx) -> None:
    """The reported bug: the press starts inside the dialog, the click lands on the scrim, the dialog must survive."""
    ctx.eval("__mount(false); __pressThenClick(__dialog);")
    assert _closed(ctx) == 0


def test_a_plain_click_on_the_scrim_still_closes_the_modal(ctx) -> None:
    """Press and click both on the scrim: the old behaviour is kept."""
    ctx.eval("__mount(false); __pressThenClick(__scrim);")
    assert _closed(ctx) == 1


def test_a_drag_select_that_starts_inside_and_ends_on_the_sheet_scrim_keeps_the_sheet_open(ctx) -> None:
    """The phone sheet has the same scrim handler, so it gets the same guard."""
    ctx.eval("__mount(true); __pressThenClick(__dialog);")
    assert _closed(ctx) == 0


def test_a_plain_click_on_the_sheet_scrim_still_closes_it(ctx) -> None:
    ctx.eval("__mount(true); __pressThenClick(__scrim);")
    assert _closed(ctx) == 1
