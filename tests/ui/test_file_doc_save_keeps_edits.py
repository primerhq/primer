"""The file editor keeps text typed while a save is out (UI-M2).

``NV_FileDoc`` (``ui/components/console/nv-file-docs.jsx``) used to clear its
draft the moment the save PUT resolved, so keystrokes typed while the request
was out were thrown away and the editor fell back to the pre-save read until
the refetch landed; a second save pressed in that window sent a second PUT
with the same etag, and the 412 it got read as a conflict with the disk; and
the Ctrl+S handler answered even when focus was in another surface.

Now a save in flight is ignored (``savingRef``); the draft clears only when
nothing was typed meanwhile (the functional ``setDraft`` compares the current
draft with the body that went out); the saved body stays on screen until the
refetched read lands (``savedRef``, cleared by an effect on ``read.data``);
and Ctrl+S acts only while the focused element's nearest file-doc root is
this doc's root (``document.activeElement`` driven in the test).

The harness is the strict mini React on the real file, with a deferred
``SH_api.fileWrite`` (the test holds the PUT in flight), a cache entry whose
``data`` identity stays put until the test lands a new read, and a window
keydown recorder.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

CONSOLE = Path(__file__).resolve().parents[2] / "ui" / "components" / "console"

PRELUDE = r"""
window.requestAnimationFrame = function () { return 0; };
window.cancelAnimationFrame = function () {};
window.__listeners = [];
window.addEventListener = function (type, fn) { if (type === "keydown") window.__listeners.push(fn); };
window.removeEventListener = function () {};
globalThis.document = { activeElement: null };
var TOASTS = [];
var CON = { wid: "ws-a", toast: function (t) { TOASTS.push(t); }, promoteDoc: function () {} };
function NV_useConsole() { return CON; }
// The useResource cache entry: snap is the stable data identity the hook
// keeps handing out until a new fetch lands (v: the next landed body).
var FILEH = {
  v: { content: "A rules", etag: "e1", encoding: "utf-8", size_bytes: 9 },
  snap: { content: "A rules", etag: "e1", encoding: "utf-8", size_bytes: 9 }
};
var WRITES = [];
var W_PENDING = [];
var SH_api = {
  keys: { file: function (wid, path) { return wid + ":" + path; } },
  fileRead: function () { return Promise.resolve(FILEH.snap); },
  fileWrite: function (wid, path, content, etag) {
    var entry = { wid: wid, path: path, content: content, etag: etag };
    WRITES.push(entry);
    return new Promise(function (res, rej) { W_PENDING.push({ entry: entry, res: res, rej: rej }); });
  },
  fileDownloadUrl: function () { return ""; }
};
window.SH_api = SH_api;
window.primerApi = {
  useResource: function (key) {
    return { data: FILEH.snap, error: null, refetch: function () { return Promise.resolve(); } };
  }
};
"""

DRIVER = r"""
// Focus is "in this editor" when the active element's nearest file-doc root
// is THIS doc's root; the seam the component checks is document.activeElement.
function __focusHere() {
  globalThis.document.activeElement = { closest: function () {
    return { getAttribute: function () { return "nv-file-doc:AGENTS.md"; } };
  } };
}
function __focusElsewhere() {
  globalThis.document.activeElement = { closest: function () { return null; } };
}
function __pressS() {
  var e = { key: "s", ctrlKey: true, metaKey: false, __p: false,
    preventDefault: function () { this.__p = true; }, stopPropagation: function () {} };
  var ls = window.__listeners;
  if (!ls.length) throw new Error("no keydown listener registered");
  ls[ls.length - 1](e);
  return e;
}
function __type(v) { MR.find("nv-file-edit").props.onChange({ target: { value: v } }); MR.rerender(); }
function __value() { return MR.find("nv-file-edit").props.value; }
function __dirty() { return !!MR.find("nv-file-dirty"); }
function __clickSave() { MR.click("nv-file-save"); }
// The Overwrite button is the last clickable child of the conflict banner.
function __clickOverwrite() {
  var el = MR.find("nv-file-conflict");
  var kids = el.children || [];
  for (var i = kids.length - 1; i >= 0; i--) {
    var k = kids[i];
    if (k && k.__el && k.props && typeof k.props.onClick === "function") { k.props.onClick(); break; }
  }
  MR.rerender();
}
function __settleWrite(i, err) {
  var w = W_PENDING[i];
  if (err) w.rej(err); else w.res();
}
function __landRead(v) { FILEH.v = v; FILEH.snap = v; MR.rerender(); }
// The component's then/catch handlers are microtasks that drain after this
// eval; the separate rerender eval then sees their state updates.
"""


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx():
    code = transpile(CONSOLE / "nv-file-docs.jsx")
    ctx = mini_react_context(code, PRELUDE)
    ctx.eval(DRIVER)
    ctx.eval('MR.mount(NV_FileDoc, { path: "AGENTS.md" });')
    return ctx


def _settle(ctx, i: int, err: bool = False) -> None:
    ctx.eval(f"__settleWrite({i}" + (", { status: 412, detail: 'changed on disk' })" if err else ")"))
    ctx.eval("MR.rerender();")


def test_a_save_puts_the_draft_with_the_etag_and_toasts() -> None:
    # Characterization: the happy path is one PUT carrying the etag and the
    # exact draft, and a "Saved" toast.
    ctx = _ctx()
    try:
        ctx.eval('__type("A rules, edited"); __clickSave();')
        assert _js(ctx, "WRITES") == [{"wid": "ws-a", "path": "AGENTS.md", "content": "A rules, edited", "etag": "e1"}]
        _settle(ctx, 0)
        assert _js(ctx, "TOASTS") == ["Saved AGENTS.md"]
        assert len(_js(ctx, "WRITES")) == 1
    finally:
        ctx.close()


def test_a_real_412_shows_the_conflict_choice() -> None:
    # Characterization: a genuine 412 (the disk really moved) still offers
    # Reload vs Overwrite, and Overwrite goes out with no etag.
    ctx = _ctx()
    try:
        ctx.eval('__type("A rules, edited"); __clickSave();')
        _settle(ctx, 0, err=True)
        assert ctx.eval('!!MR.find("nv-file-conflict")')
        assert _js(ctx, "__dirty()") is True
        ctx.eval("__clickOverwrite();")
        last = _js(ctx, "WRITES")[-1]
        assert last == {"wid": "ws-a", "path": "AGENTS.md", "content": "A rules, edited", "etag": None}
        _settle(ctx, 1)
        assert not ctx.eval('!!MR.find("nv-file-conflict")')
    finally:
        ctx.close()


def test_ctrl_s_saves_the_focused_editor() -> None:
    # Characterization: with focus inside this editor, Ctrl+S saves.
    ctx = _ctx()
    try:
        ctx.eval("__focusHere();")
        ctx.eval('__type("A rules, edited");')
        e = _js(ctx, "__pressS()")
        assert _js(ctx, "WRITES") == [{"wid": "ws-a", "path": "AGENTS.md", "content": "A rules, edited", "etag": "e1"}]
        assert e["__p"] is True
    finally:
        ctx.close()


def test_a_second_save_while_the_first_is_out_makes_one_put() -> None:
    # Ctrl+S then Save: the second must not ride the same etag out and 412
    # into a false "changed on disk" banner.
    ctx = _ctx()
    try:
        ctx.eval("__focusHere();")
        ctx.eval('__type("A rules, edited"); __pressS();')
        ctx.eval("__clickSave();")
        assert len(_js(ctx, "WRITES")) == 1, "a second PUT left while the first was out"
        ctx.eval("__settleWrite(0);")
        ctx.eval("if (W_PENDING.length > 1) __settleWrite(1, { status: 412, detail: 'changed on disk' });")
        ctx.eval("MR.rerender();")
        assert not ctx.eval('!!MR.find("nv-file-conflict")'), "the 412 of the operator's own write read as a disk conflict"
    finally:
        ctx.close()


def test_text_typed_during_a_save_survives_the_resolve() -> None:
    # The keystrokes of the request window stay in the editor, still marked
    # modified, after the PUT resolves.
    ctx = _ctx()
    try:
        ctx.eval('__type("v1"); __clickSave();')
        ctx.eval('__type("v1, more");')
        _settle(ctx, 0)
        assert _js(ctx, "__value()") == "v1, more", "the edit typed while the save was out was dropped"
        assert _js(ctx, "__dirty()") is True
    finally:
        ctx.close()


def test_the_saved_body_is_shown_until_the_refetch_lands() -> None:
    # Until the refetch answers, the editor shows what was just saved, not
    # the stale pre-save read; a later read (same or moved) replaces it.
    ctx = _ctx()
    try:
        ctx.eval('__type("v1"); __clickSave();')
        _settle(ctx, 0)
        assert _js(ctx, "__value()") == "v1", "the pre-save read shows until the refetch lands"
        ctx.eval('__landRead({ content: "v1", etag: "e2", encoding: "utf-8", size_bytes: 2 });')
        assert _js(ctx, "__value()") == "v1"
        assert _js(ctx, "__dirty()") is False
        # a concurrent write that lands after the save is not masked
        ctx.eval('__landRead({ content: "agent v2", etag: "e3", encoding: "utf-8", size_bytes: 8 });')
        assert _js(ctx, "__value()") == "agent v2"
    finally:
        ctx.close()


def test_ctrl_s_from_outside_the_editor_saves_nothing_and_does_not_prevent() -> None:
    # Focus in another surface: no PUT, and the event is left alone.
    ctx = _ctx()
    try:
        ctx.eval("__focusElsewhere();")
        ctx.eval('__type("A rules, edited");')
        e = _js(ctx, "__pressS()")
        assert _js(ctx, "WRITES") == [], "Ctrl+S saved while focus was in another surface"
        assert e["__p"] is False, "the editor preventDefaulted a Ctrl+S that was not its own"
    finally:
        ctx.close()
