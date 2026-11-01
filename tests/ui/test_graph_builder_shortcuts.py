"""The builder's shortcuts stay out of text boxes, and one Cmd-K opens one palette (UI-M15).

The builder's window ``keydown`` handler used to answer Cmd-Z wherever the key
landed, so an undo of the DRAFT fired while the operator was typing in a text
box (the drawer's sample input, the inspector's name field), and it answered
Cmd-K the same way while the shell's command palette also answered it: one
press opened the builder's add-step palette AND the shell palette. Now the
handler ignores the keys while an input, textarea or contenteditable holds
them (the browser's own undo stays), and it takes Cmd-K in the CAPTURE phase
with ``preventDefault`` so the shell's bubble handler on the same window sees
a prevented event and returns.

The harness is the strict mini React on the real builder files with
``window.addEventListener`` recording its keydown listeners (as
``test_graph_builder_cmdk_readonly.py`` does) so a test can send the keydown.
The shell's half is pinned statically: ``nv-palette.jsx`` must return on
``ev.defaultPrevented``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.ui._graph_builder_v8 import DRIVER, builder_code
from tests.ui._mini_react import mini_react_context
from tests.ui.test_graph_builder_import_shapes import BASE

PRELUDE = r"""
window.requestAnimationFrame = function () { return 0; }; window.cancelAnimationFrame = function () {};
window.__listeners = [];
window.addEventListener = function (type, fn, opts) {
  if (type === "keydown") window.__listeners.push({ fn: fn, capture: !!(opts && (opts === true || (typeof opts === "object" && !!opts.capture))) });
};
window.removeEventListener = function () {};
var TOOLS = [{ id: "ts__echo", "description": "echo", "input_schema": { type: "object", properties: { msg: { type: "string" } }, required: ["msg"] } }];
window.primerApi = {
  useResource: function () { return { data: { items: TOOLS } }; },
  useMutation: function () { return { mutate: function () {}, loading: false }; },
  usePagedList: function () { return { items: [], loading: false }; },
  Pager: function () { return null; },
  apiFetch: function () { return Promise.resolve({}); },
  useRouter: function () { return { navigate: function () {} }; },
  useEscape: function () {},
  useFocusTrap: function () {},
  focusablesOf: function () { return []; },
};
function Btn(p) { return React.createElement("button", { "data-testid": p["data-testid"], disabled: p.disabled, onClick: p.onClick }, p.children); }
function Banner(p) { return React.createElement("div", null, p.children); }
function Icon() { return null; }
function Modal(p) { return React.createElement("div", { "data-testid": "modal" }, p.title, p.children, p.footer); }
window.Btn = Btn; window.Banner = Banner; window.Icon = Icon; window.Modal = Modal;
"""

DRIVER_LOCAL = r"""
var BASE = __base__;
function sendKey(target, key, mods) {
  mods = mods || {};
  var e = {
    key: key,
    metaKey: !!mods.meta,
    ctrlKey: !!mods.ctrl,
    shiftKey: !!mods.shift,
    target: target || { tagName: "BODY" },
    __prevented: false,
    preventDefault: function () { this.__prevented = true; },
    stopPropagation: function () {},
  };
  var ls = window.__listeners;
  if (!ls.length) throw new Error("no keydown listener registered");
  ls[ls.length - 1].fn(e);
  return e;
}
function paletteOpen() { return MR.find("gb-palette") !== null; }
function dirtyShown() { return MR.findAll("gb-dirty").length === 1; }
function listenerCapture() { var ls = window.__listeners; return ls[ls.length - 1].capture; }
// A dirty draft with one undo entry: load BASE with an edited description.
function __dirty() {
  var spec = {};
  for (var k in BASE) spec[k] = BASE[k];
  spec.description = BASE.description + " (edited)";
  load(spec);
}
""".replace("__base__", json.dumps(BASE))

UI = Path(__file__).resolve().parents[2] / "ui"


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx():
    ctx = mini_react_context(builder_code(), PRELUDE)
    ctx.eval(DRIVER + "\n" + DRIVER_LOCAL)
    return ctx


def _mount_dirty(ctx) -> None:
    ctx.eval("mountBuilder(" + json.dumps(BASE) + ");")
    ctx.eval("__dirty();")
    assert _js(ctx, "dirtyShown()") is True, "the setup edit did not make the draft dirty"


def test_cmd_z_with_an_input_target_leaves_the_draft_alone() -> None:
    # The key belongs to the text box: the draft must not be undone from it.
    ctx = _ctx()
    try:
        _mount_dirty(ctx)
        ctx.eval("var e = sendKey({ tagName: 'INPUT' }, 'z', { meta: true }); MR.rerender();")
        assert _js(ctx, "dirtyShown()") is True, "Cmd-Z from an input undid the draft"
    finally:
        ctx.close()


def test_cmd_z_with_a_plain_target_still_undoes() -> None:
    # The control: outside a text box the shortcut keeps undoing the draft.
    ctx = _ctx()
    try:
        _mount_dirty(ctx)
        ctx.eval("sendKey({ tagName: 'BODY' }, 'z', { meta: true }); MR.rerender();")
        assert _js(ctx, "dirtyShown()") is False, "Cmd-Z from the page no longer undoes the draft"
    finally:
        ctx.close()


def test_cmd_k_with_an_input_target_is_left_to_the_shell() -> None:
    # While typing, the builder must neither take the shortcut nor
    # preventDefault it: the shell's palette keeps working and is the only
    # one that opens.
    ctx = _ctx()
    try:
        ctx.eval("mountBuilder(" + json.dumps(BASE) + ");")
        ctx.eval("var e = sendKey({ tagName: 'INPUT' }, 'k', { meta: true }); MR.rerender();")
        assert _js(ctx, "e.__prevented") is False, "the builder preventDefaulted a Cmd-K typed inside an input"
        assert _js(ctx, "paletteOpen()") is False, "the builder's palette opened from an input target"
    finally:
        ctx.close()


def test_cmd_k_with_a_plain_target_opens_the_builder_palette_and_prevents_default() -> None:
    # The control: outside a text box the builder takes the shortcut, and its
    # preventDefault is what keeps the shell's palette from opening too.
    ctx = _ctx()
    try:
        ctx.eval("mountBuilder(" + json.dumps(BASE) + ");")
        ctx.eval("var e = sendKey({ tagName: 'BODY' }, 'k', { meta: true }); MR.rerender();")
        assert _js(ctx, "e.__prevented") is True
        assert _js(ctx, "paletteOpen()") is True
    finally:
        ctx.close()


def test_the_builder_keydown_listener_is_registered_in_capture() -> None:
    # The ordering the shell relies on: the builder's preventDefault must land
    # before the shell palette's bubble handler on the same window, which only
    # works if the builder listens in the capture phase.
    ctx = _ctx()
    try:
        ctx.eval("mountBuilder(" + json.dumps(BASE) + ");")
        assert _js(ctx, "listenerCapture()") is True, "the builder's keydown listener is not capture: the shell palette's defaultPrevented check would never see its preventDefault"
    finally:
        ctx.close()


def test_the_shell_palette_returns_on_a_prevented_cmd_k() -> None:
    # The other half, statically: the shell's own handler must return on
    # defaultPrevented, so the press it shares with the builder opens the
    # builder's palette and nothing else.
    src = (UI / "components/console/nv-palette.jsx").read_text(encoding="utf-8")
    i = src.index("function onKey(ev)")
    block = src[i : i + 400]
    assert "defaultPrevented" in block, "the shell palette's keydown handler does not return on a prevented Cmd-K"
    assert re.search(r"addEventListener\(\s*[\"']keydown[\"']\s*,\s*onKey", src), "the shell palette lost its keydown listener"
