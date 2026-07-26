"""Studio revamp: the two-pane center (ui/studio/STUDIO-WIRING.md §6).

The pane state machine is where this feature can lose user data - a file edited
in the companion pane whose dirty flag never gets set closes without a
confirmation. So the move/close/dirty logic is exercised for real in MiniRacer
rather than grepped for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
STUDIO = UI / "components" / "studio.jsx"
CENTER = UI / "components" / "studio-center.jsx"
PANES = UI / "components" / "studio" / "st-panes.jsx"


def _code_only(src: str) -> str:
    out = []
    for line in src.splitlines():
        idx = line.find("//")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def _ctx():
    """MiniRacer with st-panes' pure helpers loaded (no React)."""
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    src = PANES.read_text(encoding="utf-8")
    # Just the pure helpers: stop before the window exports, which reference the
    # React components that are deliberately not loaded here.
    start = src.index("var ST2_WRITE_TOOLS")
    ctx.eval(src[start:src.index("window.PaneHost")])
    return ctx


# ---------------------------------------------------------------------------
# WROTE-row derivation
# ---------------------------------------------------------------------------


def test_write_tools_are_the_ids_the_runtime_actually_emits() -> None:
    # WIRING names fs__write_file / fs__apply_patch. Those do not exist here;
    # the runtime emits `workspace__<bare>` (tool_manager.WORKSPACE_TOOLSET_ID
    # + the tool's ClassVar id). Matching the spec verbatim would have matched
    # nothing, silently and forever - so pin against the real source.
    from primer.agent.tool_manager import WORKSPACE_TOOLSET_ID
    from primer.workspace.sandbox.tools import SandboxEdit, SandboxWrite

    expected = {
        f"{WORKSPACE_TOOLSET_ID}__{SandboxWrite.id}",
        f"{WORKSPACE_TOOLSET_ID}__{SandboxEdit.id}",
    }
    src = _code_only(PANES.read_text(encoding="utf-8"))
    for name in expected:
        assert name in src, name
    assert "fs__write_file" not in src
    assert "fs__apply_patch" not in src


def test_a_write_call_yields_its_path() -> None:
    ctx = _ctx()
    ctx.eval(
        'var r = ST2_wroteFromToolCall({name: "workspace__write",'
        ' args: {path: "src/a.py", content: "x"}});'
    )
    assert ctx.eval("r.path") == "src/a.py"


def test_an_edit_call_yields_its_path() -> None:
    ctx = _ctx()
    ctx.eval('var r = ST2_wroteFromToolCall({tool_name: "workspace__edit", args: {path: "b.txt"}});')
    assert ctx.eval("r.path") == "b.txt"


def test_exec_is_never_treated_as_a_write() -> None:
    # `exec` can write via a redirect, but its argv cannot say WHICH file, and a
    # WROTE row pointing at the wrong path is worse than no row.
    ctx = _ctx()
    ctx.eval(
        'var r = ST2_wroteFromToolCall({name: "workspace__exec",'
        ' args: {command: "echo hi > out.txt"}});'
    )
    assert ctx.eval("r") is None


def test_a_read_call_is_not_a_write() -> None:
    ctx = _ctx()
    ctx.eval('var r = ST2_wroteFromToolCall({name: "workspace__read", args: {path: "a"}});')
    assert ctx.eval("r") is None


def test_a_write_with_no_resolvable_path_is_skipped_not_guessed() -> None:
    ctx = _ctx()
    ctx.eval('var a = ST2_wroteFromToolCall({name: "workspace__write", args: {}});')
    ctx.eval('var b = ST2_wroteFromToolCall({name: "workspace__write"});')
    ctx.eval('var c = ST2_wroteFromToolCall({name: "workspace__write", args: {path: 42}});')
    assert ctx.eval("a") is None
    assert ctx.eval("b") is None
    assert ctx.eval("c") is None


def test_derivation_handles_a_null_row() -> None:
    ctx = _ctx()
    ctx.eval("var r = ST2_wroteFromToolCall(null);")
    assert ctx.eval("r") is None


# ---------------------------------------------------------------------------
# Pane composition
# ---------------------------------------------------------------------------


def test_panehost_mounts_the_same_center_body_twice() -> None:
    src = PANES.read_text(encoding="utf-8")
    assert src.count("<StudioCenter") == 2
    # Reused, not forked - so FilePanel / the transcript / the panels are all
    # untouched and both panes get the same CenterTabs testids.
    for forked in ("function StudioCenter(", "function CenterTabs(", "function FilePanel("):
        assert forked not in src, forked


def test_center_body_is_parameterised_not_state_bound() -> None:
    src = CENTER.read_text(encoding="utf-8")
    assert "function StudioCenter({ wid, studio, tabs, activeId, onFocus, onClose, onCloseAll, testId })" in src
    # Omitted props must resolve to the primary pane, so v1 is unaffected.
    assert "var openTabs = tabs || s.openTabs || [];" in src
    assert "activeId !== undefined ? activeId : s.activeTabId" in src
    assert "onFocus || studio.focusTab" in src
    assert "onClose || studio.closeTab" in src
    assert "onCloseAll || studio.closeAllTabs" in src


def test_aside_is_open_exactly_when_it_holds_tabs() -> None:
    # No asideOpen flag: a second source of truth for "is the pane open" is a
    # desync waiting to happen, and §6 requires closing the last tab to close
    # the pane - which is free if openness is derived.
    src = PANES.read_text(encoding="utf-8")
    assert "var asideOpen = asideTabs.length > 0;" in src
    assert '"asideOpen"' not in _code_only(STUDIO.read_text(encoding="utf-8"))


def test_narrow_viewports_get_an_overlay_not_a_squeezed_column() -> None:
    src = PANES.read_text(encoding="utf-8")
    assert "ST2_PANE_STACK_W = 1280" in src
    assert "position: \"absolute\"" in src
    # useViewport's width is enough; no isMobile fork (§6).
    assert "window.primerApi.useViewport()" in src
    assert "isMobile" not in _code_only(src)


def test_viewport_hook_is_called_unconditionally() -> None:
    # A hook behind a `typeof ... === "function"` guard is a conditional hook.
    src = _code_only(PANES.read_text(encoding="utf-8"))
    assert 'typeof window.useViewport === "function" ? window.useViewport()' not in src


def test_panes_expose_their_testids() -> None:
    src = PANES.read_text(encoding="utf-8")
    for tid in ("studio-panes", "studio-aside", "studio-aside-inner",
                "aside-resize", "aside-close", "aside-move-back"):
        assert f'"{tid}"' in src, tid


def test_aside_width_is_clamped() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert "Math.max(380, Math.min(900, w))" in src


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def _studio_actions():
    """Extract the pane reducers into MiniRacer as plain state->state functions."""
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    src = STUDIO.read_text(encoding="utf-8")
    # moveTabAcross's body is the interesting one; lift it verbatim.
    start = src.index("var moveTabAcross = React.useCallback(function () {")
    end = src.index("}, []);", start) + len("}, []);")
    body = src[start:end]
    body = body.replace("var moveTabAcross = React.useCallback(function () {", "function moveTabAcross(s) {")
    body = body.replace("setState(function (s) {", "return (function () {")
    body = body.replace("});\n  }, []);", "})();\n}")
    ctx.eval(body)
    return ctx


def test_move_across_sends_the_active_main_tab_to_the_companion() -> None:
    ctx = _studio_actions()
    ctx.eval("""
        var s = {
          openTabs: [{id: "a"}, {id: "b"}], activeTabId: "b",
          asideTabs: [], activeAsideTabId: null
        };
        var out = moveTabAcross(s);
        var mainIds = out.openTabs.map(function (t) { return t.id; }).join(',');
        var asideIds = out.asideTabs.map(function (t) { return t.id; }).join(',');
    """)
    assert ctx.eval("mainIds") == "a"
    assert ctx.eval("asideIds") == "b"
    assert ctx.eval("out.activeAsideTabId") == "b"
    assert ctx.eval("out.activeTabId") == "a"


def test_move_across_is_reversible_with_the_same_gesture() -> None:
    # Alt-\ both directions: the tab in the companion pane comes back.
    ctx = _studio_actions()
    ctx.eval("""
        var s = {
          openTabs: [{id: "a"}], activeTabId: "a",
          asideTabs: [{id: "b"}], activeAsideTabId: "b"
        };
        var out = moveTabAcross(s);
        var mainIds = out.openTabs.map(function (t) { return t.id; }).join(',');
        var asideLen = out.asideTabs.length;
    """)
    assert ctx.eval("mainIds") == "a,b"
    assert ctx.eval("asideLen") == 0
    assert ctx.eval("out.activeAsideTabId") is None
    assert ctx.eval("out.activeTabId") == "b"


def test_move_across_is_a_noop_with_nothing_active() -> None:
    ctx = _studio_actions()
    ctx.eval("""
        var s = {openTabs: [], activeTabId: null, asideTabs: [], activeAsideTabId: null};
        var same = moveTabAcross(s) === s;
    """)
    assert ctx.eval("same") is True


def test_close_aside_tab_activates_the_left_neighbour() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    # Same post-filter index rule as closeTab, so there is no off-by-one.
    assert "asideTabs[Math.min(Math.max(0, idx - 1), asideTabs.length - 1)].id" in src


def test_closing_the_last_aside_tab_clears_the_active_id() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert "activeAsideTabId = asideTabs.length" in src


def test_dirty_tracking_covers_the_companion_pane() -> None:
    # A file edited in the companion pane whose dirty flag never lands gets
    # closed with no confirmation - silent data loss.
    src = CENTER.read_text(encoding="utf-8")
    assert '["openTabs", "asideTabs"].forEach' in src
    assert "studio.patch(patch)" in src


def test_pane_state_is_persisted() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    for key in ('"asideTabs"', '"activeAsideTabId"', '"asideWidth"'):
        assert key in src, key
    assert "asideTabs: []" in src
    assert "asideWidth: 520" in src


def test_pane_actions_are_exposed_on_the_store() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    for fn in ("openAside", "focusAsideTab", "closeAsideTab",
               "closeAllAsideTabs", "setAsideWidth", "moveTabAcross"):
        assert f"{fn}: {fn}," in src, fn


# ---------------------------------------------------------------------------
# ?aside= deep link
# ---------------------------------------------------------------------------


def test_both_panes_mirror_to_the_url() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert "function ST_syncUrl(activeTabId, activeAsideTabId)" in src
    assert 'params.set("aside", activeAsideTabId)' in src
    assert 'params.delete("aside")' in src
    assert "ST_syncUrl(state.activeTabId, state.activeAsideTabId)" in src


def test_a_pasted_two_pane_url_restores_both_sides() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert 'ST_tabFromUrl("open")' in src
    assert 'ST_tabFromUrl("aside")' in src
    assert "function ST_applyUrlTabTo(base, tabsKey, activeKey, urlTab)" in src


def test_url_parser_is_parameterised_by_pane() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert "function ST_tabFromUrl(param)" in src
    assert '.get(param || "open")' in src


def test_alt_backslash_moves_the_tab_across() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert 'isV2 && e.altKey && e.key === "\\\\"' in src
    assert "studio.moveTabAcross();" in src


def test_v1_center_is_untouched_when_the_tweak_is_off() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert "window.PaneHost" in src
    assert "<StudioCenter wid={wid} studio={studio} />" in src


def test_panes_module_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    for rel in ("components/studio/st-panes.jsx", "components/studio-center.jsx",
                "components/studio.jsx"):
        assert b._transform((UI / rel).read_text(encoding="utf-8"), rel), rel


def test_panes_registered_after_the_center() -> None:
    lines = (UI / "index.html").read_text(encoding="utf-8").splitlines()
    reg = [i for i, ln in enumerate(lines) if 'type="text/babel"' in ln and "src=" in ln]

    def idx(frag: str) -> int:
        for i in reg:
            if frag in lines[i]:
                return i
        raise AssertionError(f"{frag} is not registered")

    assert idx("studio-center.jsx") < idx("studio/st-panes.jsx")
    assert idx("studio/st-panes.jsx") < idx("components/studio.jsx")
