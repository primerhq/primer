"""An unsaved file draft survives a tab switch, shows on its tab, and closing the tab asks first (gap review GU-6).

Each tab group renders only its active tab's document, so selecting another tab unmounts the file doc. Its draft used to be component state and was
lost with it, with no mark on the tab and no prompt on close. The draft now lives in a module-level map keyed by ``wid + ":" + path`` (the key of
gap review GU-1), which outlives the unmount; the tab bar marks a tab with a draft; closing it asks first.

The real ``nv-file-docs.jsx``, ``nv-studio.jsx``, ``nv-tab-groups.jsx`` and ``tab-group-model.js`` run in V8 on the mini React.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
MODEL = (ROOT / "ui" / "foundation" / "tab-group-model.js").read_text(encoding="utf-8")

PRELUDE = r"""
var TOASTS = [], WRITES = [], CONFIRMS = [], ANSWER = true;
var CON = { wid: "ws-a", toast: function (m) { TOASTS.push(m); }, promoteDoc: function () {}, registry: { get: function () { return null; } } };
function NV_useConsole() { return CON; }
var FILES = {
  "ws-a:notes.md": { content: "v1", etag: "e1" },
  "ws-b:notes.md": { content: "other", etag: "e9" },
  "ws-a:other.md": { content: "o1", etag: "e2" },
};
window.addEventListener = function () {};
window.removeEventListener = function () {};
window.primerApi = {
  useResource: function (key) { return { data: FILES[key] || null, error: null, refetch: function () {} }; },
};
var SH_api = {
  keys: { file: function (wid, path) { return wid + ":" + path; } },
  fileRead: function () { return new Promise(function () {}); },
  fileWrite: function (wid, path, content, etag) { WRITES.push([wid, path, content, etag]); return Promise.resolve({}); },
  fileDownloadUrl: function () { return ""; },
};
window.SH_api = SH_api;
window.NV_SessionDoc = function () { return React.createElement("div", { "data-testid": "session-doc" }); };
window.confirmDialog = function (o) { CONFIRMS.push(o); return Promise.resolve(ANSWER); };
"""

HOST = r"""
var M = TG_init({ groupId: "g1" });
function open(kind, ref) { M = TG_openTab(M, { kind: kind, ref: ref }, { promote: true }); }
function Host() {
  return React.createElement(NV_TabGroups, {
    model: M,
    onModelChange: function (next) { M = next; },
    renderDoc: function (tab) { return NV_renderStudioDoc(CON, tab); },
    resolveSessionMeta: function () { return null; },
  });
}
function show() { MR.rerender(); }
function click(testid) { var out = MR.find(testid).props.onClick({ stopPropagation: function () {} }); show(); return out; }
function type(value) { MR.find("nv-file-edit").props.onChange({ target: { value: value } }); show(); }
function editorValue() { return MR.find("nv-file-edit").props.value; }
"""

NOTES = "nv-tg-tab:file:notes.md"


@pytest.fixture
def ctx():
    code = "\n".join([transpile(CONSOLE / "nv-file-docs.jsx"), transpile(CONSOLE / "nv-studio.jsx"),
                      transpile(CONSOLE / "nv-tab-groups.jsx"), MODEL])
    c = mini_react_context(code, PRELUDE)
    c.eval(HOST)
    c.eval("open('file', 'notes.md'); open('session', 's1'); MR.mount(Host, {}); click('" + NOTES + "');")
    yield c
    c.close()


def _settle(ctx) -> None:
    for _ in range(4):
        ctx.eval("void 0;")
        ctx.eval("MR.rerender();")


def test_the_draft_survives_a_switch_to_another_tab_and_back(ctx) -> None:
    ctx.eval("type('v1 plus an hour of edits');")
    assert ctx.eval("!!MR.find('nv-file-dirty')")
    ctx.eval("click('nv-tg-tab:session:s1');")
    assert ctx.eval("!!MR.find('session-doc')")
    ctx.eval("click('" + NOTES + "');")
    assert ctx.eval("editorValue()") == "v1 plus an hour of edits"
    assert ctx.eval("!!MR.find('nv-file-dirty')")


def test_the_tab_of_a_file_with_a_draft_shows_a_dirty_mark_until_it_is_saved(ctx) -> None:
    assert not ctx.eval("!!MR.find('nv-tg-tab-dirty:file:notes.md')")
    ctx.eval("type('edited');")
    assert ctx.eval("!!MR.find('nv-tg-tab-dirty:file:notes.md')")
    ctx.eval("click('nv-file-save');")
    _settle(ctx)
    assert json.loads(ctx.eval("JSON.stringify(WRITES)")) == [["ws-a", "notes.md", "edited", "e1"]]
    assert not ctx.eval("!!MR.find('nv-tg-tab-dirty:file:notes.md')")
    ctx.eval("click('nv-tg-tab:session:s1'); click('" + NOTES + "');")
    assert ctx.eval("!!MR.find('nv-file-dirty')") is False          # a saved draft does not come back


def test_a_draft_of_one_workspace_never_shows_in_another(ctx) -> None:
    ctx.eval("type('draft in a');")
    ctx.eval("CON = Object.assign({}, CON, { wid: 'ws-b' }); show();")
    assert ctx.eval("editorValue()") == "other" and not ctx.eval("!!MR.find('nv-file-dirty')")
    assert not ctx.eval("!!MR.find('nv-tg-tab-dirty:file:notes.md')")
    ctx.eval("CON = Object.assign({}, CON, { wid: 'ws-a' }); show();")
    assert ctx.eval("editorValue()") == "draft in a"


def test_closing_a_tab_with_a_draft_asks_first_and_keeps_it_when_the_answer_is_no(ctx) -> None:
    ctx.eval("type('edited'); ANSWER = false; click('nv-tg-tab-close:file:notes.md');")
    _settle(ctx)
    confirms = json.loads(ctx.eval("JSON.stringify(CONFIRMS)"))
    assert len(confirms) == 1 and "notes.md" in confirms[0]["message"] and confirms[0]["danger"] is True
    assert ctx.eval("M.groups[0].tabs.length") == 2 and ctx.eval("editorValue()") == "edited"


def test_closing_a_tab_with_a_draft_after_yes_closes_it_and_drops_the_draft(ctx) -> None:
    ctx.eval("type('edited'); click('nv-tg-tab-close:file:notes.md');")
    _settle(ctx)
    assert ctx.eval("M.groups[0].tabs.length") == 1
    ctx.eval("open('file', 'notes.md'); show();")
    assert ctx.eval("editorValue()") == "v1" and not ctx.eval("!!MR.find('nv-file-dirty')")


def test_closing_a_clean_tab_does_not_ask(ctx) -> None:
    ctx.eval("click('nv-tg-tab-close:file:notes.md');")
    _settle(ctx)
    assert ctx.eval("CONFIRMS.length") == 0 and ctx.eval("M.groups[0].tabs.length") == 1
