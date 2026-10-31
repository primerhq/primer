"""A file tab's draft never crosses a workspace switch (gap review GU-1).

Tabs are global across workspaces, and a file tab's ref is its path only. The file doc (and the draft it holds) is keyed by the
workspace and the path, so switching ``con.wid`` gives the tab a fresh doc reading the new workspace's file: an unsaved draft
from workspace A can never be PUT over the same path in workspace B. Mounts the real ``nv-studio.jsx`` + ``nv-file-docs.jsx``.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

CONSOLE = Path(__file__).resolve().parents[2] / "ui" / "components" / "console"

PRELUDE = r"""
var CON = { wid: "ws-a", toast: function () {}, promoteDoc: function () {}, registry: { get: function () { return null; } } };
function NV_useConsole() { return CON; }
var FILES = {
  "ws-a:AGENTS.md": { content: "A rules", etag: "etag-a" },
  "ws-b:AGENTS.md": { content: "B rules", etag: "etag-b" },
};
var WRITES = [];
window.addEventListener = function () {};
window.removeEventListener = function () {};
window.primerApi = {
  useResource: function (key) { return { data: FILES[key] || null, error: null, refetch: function () {} }; },
};
var SH_api = {
  keys: { file: function (wid, path) { return wid + ":" + path; } },
  fileRead: function () { return new Promise(function () {}); },
  fileWrite: function (wid, path, content, etag) { WRITES.push([wid, path, content, etag]); return new Promise(function () {}); },
  fileDownloadUrl: function () { return ""; },
};
window.SH_api = SH_api;
function Host(props) { return NV_renderStudioDoc(CON, props.tab); }
"""

TAB = json.dumps({"id": "file:AGENTS.md", "kind": "file", "ref": "AGENTS.md"})


def _ctx():
    code = transpile(CONSOLE / "nv-file-docs.jsx") + "\n" + transpile(CONSOLE / "nv-studio.jsx")
    ctx = mini_react_context(code, PRELUDE)
    ctx.eval(f"MR.mount(Host, {{ tab: {TAB} }});")
    ctx.eval('MR.find("nv-file-edit").props.onChange({ target: { value: "A rules, edited" } }); MR.rerender();')
    return ctx


def _writes(ctx) -> list:
    return json.loads(ctx.eval("JSON.stringify(WRITES)"))


def test_a_draft_from_workspace_a_is_never_saved_into_workspace_b() -> None:
    ctx = _ctx()
    try:
        # workspace.switch: only con.wid changes; the tab model (and so the tab) is untouched.
        ctx.eval('CON = Object.assign({}, CON, { wid: "ws-b" }); MR.rerender();')
        assert ctx.eval('MR.find("nv-file-edit").props.value') == "B rules"
        assert not ctx.eval('!!MR.find("nv-file-dirty")')
        assert ctx.eval('MR.find("nv-file-save").props.disabled') is True
        ctx.eval('MR.click("nv-file-save");')
        assert _writes(ctx) == []
    finally:
        ctx.close()


def test_editing_and_saving_in_one_workspace_is_unchanged() -> None:
    ctx = _ctx()
    try:
        assert ctx.eval('MR.find("nv-file-edit").props.value') == "A rules, edited"
        assert ctx.eval('!!MR.find("nv-file-dirty")')
        ctx.eval('MR.click("nv-file-save");')
        assert _writes(ctx) == [["ws-a", "AGENTS.md", "A rules, edited", "etag-a"]]
    finally:
        ctx.close()
