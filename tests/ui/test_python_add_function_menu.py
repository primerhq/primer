"""The Python editor's "Add function" menu is a layer of the Escape stack (board task 01a125ec-4e03, found in the review of #728).

``PY_AddFunction`` opens a menu of scaffolds under its button. It was not on the console's Escape stack (``ui/foundation/escape-stack.js``), so with the menu open ONE Escape closed the whole toolsets overlay and the code
that was changed and not saved went with it (verified in a browser at the head of #728 and at its red commit: the bug was there before). The menu answers Escape like the console's own menus do
(``NV_useMenuDismiss``): it is a layer only while it is open, an Escape closes it and nothing under it, and focus goes back to the button that opened it. Which layer is on top in a browser is
``tests/ui_e2e/test_python_add_function_menu_journey.py``.

The REAL ``PY_AddFunction`` runs in V8 on the mini React. The mini React has no DOM, so the button's focus is the journey's; here the layer is read: when it is registered, what answers it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]

_PRELUDE = """
window.addEventListener = function () {}; window.removeEventListener = function () {};
var __layers = [];                       // every useEscape call, in order: { handler, active }
window.primerApi = { useEscape: function (handler, active) { __layers.push({ handler: handler, active: active === undefined ? true : !!active }); } };
window.PY_SCAFFOLDS = [{ id: "plain", label: "A plain tool", hint: "h", source: "def plain(): pass\\n" }];
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], onClick: p.onClick }, p.children); }
var __inserted = [];
"""


@pytest.fixture
def menu():
    ctx = mini_react_context(transpile(ROOT / "ui" / "components" / "toolsets" / "python-editor.jsx"), _PRELUDE)
    ctx.eval("MR.mount(PY_AddFunction, { onInsert: function (s) { __inserted.push(s); } });")
    try:
        yield ctx
    finally:
        ctx.close()


def _active_now(ctx) -> bool:
    return bool(ctx.eval("__layers.length ? __layers[__layers.length - 1].active : false"))


def test_a_closed_menu_is_not_a_layer_of_the_escape_stack(menu) -> None:
    """With no menu open the Escape belongs to whatever is under it (the overlay), exactly as before."""
    assert not _active_now(menu)


def test_an_open_menu_is_a_layer_and_a_closed_one_is_not(menu) -> None:
    menu.eval("MR.click('python-add-function');")
    assert menu.eval("MR.find('python-add-function-menu') !== null") is True
    assert _active_now(menu), "the open menu is not on the Escape stack: one Escape would close the overlay under it"
    menu.eval("MR.click('python-add-function');")                      # the button toggles it shut
    assert menu.eval("MR.find('python-add-function-menu') === null") is True
    assert not _active_now(menu)


def test_an_escape_closes_the_menu_and_handles_the_key(menu) -> None:
    """The layer's handler gets the event: it closes the menu and prevents the default, so nothing else treats the key as its own."""
    menu.eval("MR.click('python-add-function');")
    assert menu.eval("__layers.length") > 0, "the menu never registered an Escape layer"
    menu.eval("var __ev = { prevented: 0, preventDefault: function () { this.prevented += 1; } }; __layers[__layers.length - 1].handler(__ev); MR.rerender();")
    assert menu.eval("MR.find('python-add-function-menu') === null") is True, "Escape did not close the menu"
    assert menu.eval("__ev.prevented") == 1
    assert not _active_now(menu), "a closed menu stays on the stack"


def test_choosing_a_scaffold_still_inserts_it_and_closes_the_menu(menu) -> None:
    menu.eval("MR.click('python-add-function'); MR.click('python-scaffold-plain');")
    assert json.loads(menu.eval("JSON.stringify(__inserted)")) == ["def plain(): pass\n"]
    assert menu.eval("MR.find('python-add-function-menu') === null") is True
    assert not _active_now(menu)
