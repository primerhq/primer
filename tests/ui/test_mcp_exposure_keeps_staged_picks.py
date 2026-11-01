"""Staged MCP tool-exposure picks survive a row re-read (UI-M14).

``MC_ToolsPanel`` (``ui/components/mcp.jsx``) memoised its persisted
``allowed_tools`` with the array itself as the dependency, so any new row
object - the master toggle's refetch, a poll-driven re-read after another
admin's edit - rebuilt the Set and the resync effect wiped the operator's
staged picks even when the list was unchanged, with Save back to disabled.

Now the memo depends on the sorted content, so the Set keeps its identity
while the content is unchanged and the staged picks survive; a genuinely
changed list still re-seeds the draft.

MiniRacer on the real file with a stubbed ``window.primerApi`` (``apiFetch``
recording the save PUT, a ``Pager`` stub) and an ``exposure`` resource whose
``data`` the test re-reads with fresh row objects.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

MCP = Path(__file__).resolve().parents[2] / "ui" / "components" / "mcp.jsx"

PRELUDE = r"""
globalThis.Icon = function () { return null; };
globalThis.Btn = function (props) {
  return React.createElement("button", {
    "data-testid": props["data-testid"], onClick: props.onClick,
    disabled: props.disabled || !!props.busy
  }, props.children);
};
globalThis.Banner = function () { return null; };
var EXPOSURE = {
  data: { enabled: true, allowed_tools: ["t/a"], updated_by: "admin",
          updated_at: "2026-01-01T00:00:00Z" },
  refetch: function () { EXPOSURE.refetches = (EXPOSURE.refetches || 0) + 1; }
};
var AVAILABLE = { data: { items: [
  { scoped_id: "t/a", toolset_id: "t", description: "a", exposable: true },
  { scoped_id: "t/b", toolset_id: "t", description: "b", exposable: true }
] } };
var PUTS = [];
window.primerApi = {
  apiFetch: function (method, url, payload) {
    if (method === "PUT") { PUTS.push(payload); return Promise.resolve({}); }
    return Promise.resolve({});
  },
  Pager: function () { return null; }
};
"""

DRIVER = r"""
function __chk(scoped) {
  var row = MR.find("tool-row-" + scoped);
  return row.children[0].children[0];
}
function __clickChk(scoped) { __chk(scoped).props.onChange(); MR.rerender(); }
function __checked(scoped) { return __chk(scoped).props.checked; }
function __saveEnabled() { return !MR.find("save-allowed-btn").props.disabled; }
function __save() { MR.click("save-allowed-btn"); }
function __rowReload(allowed) {
  // A re-read: a NEW row object (a fresh allowed_tools array even when the
  // content is unchanged), as the master toggle's or the poll's refetch
  // produces.
  EXPOSURE.data = { enabled: true, allowed_tools: allowed,
                    updated_by: "admin", updated_at: "2026-01-01T01:00:00Z" };
  MR.rerender();
}
"""


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _ctx():
    ctx = mini_react_context(transpile(MCP), PRELUDE)
    ctx.eval(DRIVER)
    ctx.eval("MR.mount(MC_ToolsPanel, { exposure: EXPOSURE, available: AVAILABLE });")
    return ctx


def test_staging_a_tool_enables_save() -> None:
    # Characterization: ticking a tool stages it and enables Save.
    ctx = _ctx()
    try:
        assert _js(ctx, '__checked("t/b")') is False
        ctx.eval('__clickChk("t/b");')
        assert _js(ctx, '__checked("t/b")') is True
        assert _js(ctx, "__saveEnabled()") is True
    finally:
        ctx.close()


def test_a_row_reread_with_the_same_tools_keeps_the_staged_picks() -> None:
    # A re-read that lands the same list (new row object, same content) must
    # not wipe the picks or disable Save.
    ctx = _ctx()
    try:
        ctx.eval('__clickChk("t/b");')
        ctx.eval('__rowReload(["t/a"]);')
        assert _js(ctx, '__checked("t/b")') is True, (
            "the re-read wiped the staged pick though the list was unchanged"
        )
        assert _js(ctx, "__saveEnabled()") is True
    finally:
        ctx.close()


def test_a_row_reread_with_changed_tools_resyncs_the_draft() -> None:
    # Characterization: a genuinely changed list still re-seeds the draft -
    # here the pick the operator made is now persisted, so Save disables.
    ctx = _ctx()
    try:
        ctx.eval('__clickChk("t/b");')
        ctx.eval('__rowReload(["t/a", "t/b"]);')
        assert _js(ctx, '__checked("t/b")') is True
        assert _js(ctx, "__saveEnabled()") is False, (
            "the draft did not resync to the now-persisted pick"
        )
    finally:
        ctx.close()


def test_a_save_puts_the_sorted_staged_set_and_refetches() -> None:
    # Characterization: Save PUTs the staged set (sorted) and refetches the
    # row.
    ctx = _ctx()
    try:
        ctx.eval('__clickChk("t/b"); __save();')
        assert _js(ctx, "PUTS")[0]["allowed_tools"] == ["t/a", "t/b"]
        ctx.eval("MR.rerender();")
        assert _js(ctx, "EXPOSURE.refetches") == 1
    finally:
        ctx.close()
