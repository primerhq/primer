"""Cmd-K opens the add-step palette only where a step can actually be added (found by the #693 review, 2026-10-09).

A graph a harness manages (``loaded.harness_id``) draws the builder READ-ONLY: the banner says direct edits are blocked, and the dispatch is a no-op. The outline's "+ Add a step" button is already hidden in that mode, but the Cmd-K (Ctrl-K) shortcut was not guarded, so a keypress opened the palette and every step picked from it was silently dropped: the user picks a step and nothing happens.

The harness is the strict mini React on the real builder files, with one difference from the shared ``_graph_builder_v8`` prelude: ``window.addEventListener`` RECORDS its listeners, so a test can send the keydown the builder's ``useEffect`` registers on ``window``. The canvas has no palette entry point, so no guard is needed there; the outline button's existing ``!readOnly`` guard is pinned (hidden read-only, shown editable) so the shortcut is the last unguarded entry point.

Production order matters: the builder mounts while the graph fetch is still in flight (editable), and ``harness_id`` arrives on a LATER re-render, so ``readOnly`` turns true after mount. The keydown effect must re-run on that flip (``readOnly`` is one of its deps), or the stale mount-time closure keeps opening the palette read-only - pinned by mounting editable and re-rendering with ``harness_id``.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._graph_builder_v8 import builder_code
from tests.ui._mini_react import mini_react_context
from tests.ui.test_graph_builder_import_shapes import BASE

PRELUDE = r"""
window.requestAnimationFrame = function () { return 0; }; window.cancelAnimationFrame = function () {};
window.__listeners = {};
window.addEventListener = function (type, fn) { (window.__listeners[type] = window.__listeners[type] || []).push(fn); };
window.removeEventListener = function (type, fn) { var ls = window.__listeners[type] || []; var i = ls.indexOf(fn); if (i >= 0) ls.splice(i, 1); };
var TOOLS = [{ id: "ts__echo", description: "echo", input_schema: { type: "object", properties: { msg: { type: "string" } }, required: ["msg"] } }];
window.primerApi = {
  useResource: function () { return { data: { items: TOOLS } }; },
  useMutation: function () { return { mutate: function () {}, loading: false }; },
  usePagedList: function () { return { items: [], loading: false }; },
  Pager: function () { return null; },
  apiFetch: function () { return Promise.resolve({}); },
  useRouter: function () { return { navigate: function () {} }; },
};
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, onClick: p.onClick }, p.children); }
function Banner(p) { return React.createElement("div", null, p.children); }
function Icon() { return null; }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.title, p.children, p.footer); }
window.Btn = Btn; window.Banner = Banner; window.Icon = Icon; window.Modal = Modal;
"""

DRIVER = r"""
var NOOP = function () {};
function mountBuilder(loaded) { MR.mount(GB_Builder, { graphId: "g", loaded: loaded, pushToast: NOOP }); }
function sendKey(key, mods) {
  mods = mods || {};
  var e = { key: key, metaKey: !!mods.meta, ctrlKey: !!mods.ctrl, shiftKey: false, preventDefault: function () {} };
  (window.__listeners["keydown"] || []).forEach(function (fn) { fn(e); });
}
function paletteOpen() { return MR.find("gb-palette") !== null; }
function texts() { return JSON.stringify(MR.texts()); }
"""


@pytest.fixture
def ctx():
    c = mini_react_context(builder_code(), PRELUDE)
    c.eval(DRIVER)
    try:
        yield c
    finally:
        c.close()


def _mount(ctx, harness: bool) -> None:
    loaded = dict(BASE)
    if harness:
        loaded["harness_id"] = "harness-1"
    ctx.eval("mountBuilder(" + json.dumps(loaded) + ")")


def _press(ctx, key: str = "k", meta: bool = False, ctrl: bool = False) -> None:
    ctx.eval(f"sendKey({json.dumps(key)}, {{ meta: {str(meta).lower()}, ctrl: {str(ctrl).lower()} }}); MR.rerender();")


def test_a_read_only_builder_is_read_only(ctx) -> None:
    """The control: harness_id really puts the builder in the blocked mode, so the guard below has something to bite on."""
    _mount(ctx, harness=True)
    text = " ".join(json.loads(ctx.eval("texts()")))
    assert "managed by harness" in text, "the banner is missing: the mount is not read-only"
    assert ctx.eval('MR.find("gb-outline-add") !== null') is False, "the outline's add button is not hidden read-only"


@pytest.mark.parametrize("modifier", ["meta", "ctrl"], ids=["cmd_k", "ctrl_k"])
def test_cmd_k_does_not_open_the_palette_in_a_read_only_builder(ctx, modifier: str) -> None:
    _mount(ctx, harness=True)
    _press(ctx, meta=modifier == "meta", ctrl=modifier == "ctrl")
    assert ctx.eval("paletteOpen()") is False, "the palette opened in a read-only builder: a step picked from it is silently dropped"
    assert ctx.eval('MR.find("gb-palette") !== null') is False


def test_cmd_k_still_opens_the_palette_in_an_editable_builder(ctx) -> None:
    """The control: the guard must not kill the shortcut, only its read-only reach."""
    _mount(ctx, harness=False)
    _press(ctx, meta=True)
    assert ctx.eval("paletteOpen()") is True, "the shortcut no longer opens the palette in an editable builder"


def test_a_plain_k_does_not_open_the_palette(ctx) -> None:
    _mount(ctx, harness=False)
    _press(ctx, meta=False, ctrl=False)
    assert ctx.eval("paletteOpen()") is False


def test_the_outline_add_button_shows_in_an_editable_builder(ctx) -> None:
    _mount(ctx, harness=False)
    assert ctx.eval('MR.find("gb-outline-add") !== null') is True


def test_cmd_k_does_not_open_the_palette_after_the_builder_becomes_read_only(ctx) -> None:
    """Production order: mounted editable, ``harness_id`` arrives on a later re-render. The keydown effect must re-run on that flip: with ``readOnly`` missing from its deps, the stale mount-time closure (readOnly false) still opens the palette."""
    _mount(ctx, harness=False)
    ro = dict(BASE)
    ro["harness_id"] = "harness-1"
    ctx.eval("MR.rerender({ graphId: 'g', loaded: " + json.dumps(ro) + ", pushToast: NOOP });")
    assert "managed by harness" in " ".join(json.loads(ctx.eval("texts()"))), "the re-render did not turn the builder read-only"
    _press(ctx, meta=True)
    assert ctx.eval("paletteOpen()") is False, "the palette opened after the builder turned read-only: the keydown effect kept the stale mount-time closure"
