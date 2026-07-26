"""Studio revamp: the investigate dock (ui/studio/STUDIO-WIRING.md §8, §2.1).

The dock re-houses the tap and the terminal and adds a derived Problems list.
It replaces the right rail, but ONLY under the studioV2 tweak - that tweak is a
runtime flag, so one build has to serve both shells and both sets of persisted
state. These tests pin that coexistence, which is the part most likely to rot.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
STUDIO_JSX = UI / "components" / "studio.jsx"
DOCK = UI / "components" / "studio" / "st-dock.jsx"


def _studio() -> str:
    return STUDIO_JSX.read_text(encoding="utf-8")


def _dock() -> str:
    return DOCK.read_text(encoding="utf-8")


def test_dock_module_exists_and_exports() -> None:
    assert DOCK.exists()
    src = _dock()
    assert "function InvestigateDock(" in src
    assert "window.InvestigateDock" in src


def test_dock_rehouses_the_tap_and_terminal_verbatim() -> None:
    # Re-housed, not rebuilt: same components, same props.
    src = _dock()
    assert "<WorkspaceTap" in src
    assert "fillHeight" in src
    assert "<TerminalPanel" in src


def test_dock_exposes_its_tab_testids() -> None:
    src = _dock()
    for tid in ("investigate-dock", "problems-list"):
        assert f'"{tid}"' in src, tid
    # Tab ids are composed (`"dock-tab-" + id`) so one component renders them
    # all; assert the prefix plus each id that is passed in.
    assert '"dock-tab-" + id' in src
    for tab in ('id="events"', 'id="problems"'):
        assert tab in src, tab
    # The terminal tab id must resolve even with several terminals open, so
    # test_studio_terminal.py has one stable locator.
    assert '"terminal"' in src and '"terminal-"' in src


def test_problems_is_derived_and_needs_no_endpoint() -> None:
    # Failed runs come from the bucket language; errors from the shared tap.
    src = _dock()
    assert "ST2_bucketOf" in src
    assert '"broken"' in src
    assert "useWorkspaceTapListener" in src
    assert "EventSource" not in src


def test_dock_does_not_name_urls_directly() -> None:
    src = _dock()
    for marker in ('apiFetch("GET"', 'apiFetch("POST"'):
        assert marker not in src


# ---------------------------------------------------------------------------
# §2.1 - state model, and the coexistence the runtime tweak demands
# ---------------------------------------------------------------------------


def test_dock_state_keys_are_added_and_persisted() -> None:
    src = _studio()
    for key in ('"dockOpen"', '"dockTab"', '"dockHeight"'):
        assert key in src, key
    assert "dockOpen: false" in src
    assert "dockTab: \"events\"" in src


def test_v1_state_keys_survive_because_the_tweak_is_a_runtime_flag() -> None:
    # The plan originally said to delete these. That would break the v1 shell,
    # which is still reachable at runtime whenever studioV2 is off.
    src = _studio()
    assert '"terminalOpen"' in src
    assert '"debugOpen"' in src
    assert '"terminalHeight"' in src


def test_terminal_state_migrates_into_the_dock_once() -> None:
    # Switching the tweak on must not silently close a terminal the operator
    # had open.
    src = _studio()
    assert "keep.dockOpen === undefined" in src
    assert "parsed.terminalOpen" in src
    assert "keep.dockHeight === undefined" in src
    assert "parsed.terminalHeight" in src


def test_dock_actions_exist_and_clamp_height() -> None:
    src = _studio()
    for fn in ("toggleDock", "setDockTab", "setDockHeight"):
        assert fn + ":" in src, fn
    assert "Math.max(120" in src


def test_the_right_column_is_gone_under_v2_only() -> None:
    src = _studio()
    # Rendered behind !isV2, never removed outright.
    assert "{!isV2 && (" in src
    assert "<StudioActivity" in src
    assert "InvestigateDock" in src


def test_ctrl_backtick_and_the_resize_follow_the_active_shell() -> None:
    src = _studio()
    assert "if (isV2) studio.toggleDock(); else studio.toggleTerminal();" in src
    assert "if (isV2) studio.setDockHeight(h); else studio.setTerminalHeight(h);" in src
    assert '"dock-resize"' in src


def test_right_width_css_var_collapses_under_v2() -> None:
    src = _studio()
    assert "!isV2 && s.debugOpen" in src


def test_dock_is_registered_before_studio_in_index_html() -> None:
    lines = (UI / "index.html").read_text(encoding="utf-8").splitlines()
    order = [i for i, ln in enumerate(lines) if 'type="text/babel"' in ln and "src=" in ln]

    def idx(frag: str) -> int:
        for i in order:
            if frag in lines[i]:
                return i
        raise AssertionError(f"{frag} is not registered")

    assert idx("studio/st-dock.jsx") < idx("components/studio.jsx")


def test_studio_bundle_still_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    for rel in ("components/studio.jsx", "components/studio/st-dock.jsx"):
        code = b._transform((UI / rel).read_text(encoding="utf-8"), rel)
        assert code, rel
