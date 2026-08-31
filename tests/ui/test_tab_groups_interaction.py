"""US-007 R2 phase 1 review finding #1, fixed in phase 2 (step 0).

nv-tab-groups.jsx has no jsdom in the py toolchain (confirmed alongside the
other nv-*.jsx statics, e.g. test_admin_users_page.py), so this is a static
source guard like its siblings (test_console_shell.py etc.), not a rendered
click simulation.

The group wrapper's onClick (focus-on-click) and a tab's onClick/
onDoubleClick (select/promote) all read the SAME model prop. Clicking or
double-clicking a tab in an UNFOCUSED group bubbles the tab's click up to
the group wrapper, which re-derives its own model from that same pre-click
closure and, called right after selectTab/promoteTab, can overwrite what
they just dispatched with a stale-model result - reverting the just-clicked
tab's activation. closeTab already stopped propagation for exactly this
reason; select and promote did not.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (
    ROOT / "ui" / "components" / "console" / "nv-tab-groups.jsx"
).read_text(encoding="utf-8")


def _fn_body(name: str) -> str:
    m = re.search(r"function " + name + r"\([^)]*\)\s*\{([\s\S]*?)\n  \}", SRC)
    assert m, f"{name} not found"
    return m.group(1)


def test_select_tab_stops_propagation_before_dispatching() -> None:
    body = _fn_body("selectTab")
    assert "ev.stopPropagation()" in body
    # Must guard THEN dispatch, matching closeTab's order - stopping
    # propagation after the model change would be too late.
    assert body.index("stopPropagation") < body.index("change(")


def test_promote_tab_stops_propagation_before_dispatching() -> None:
    body = _fn_body("promoteTab")
    assert "ev.stopPropagation()" in body
    assert body.index("stopPropagation") < body.index("change(")


def test_close_tab_still_stops_propagation() -> None:
    # Regression guard: the pattern this fix was modeled on must not regress.
    body = _fn_body("closeTab")
    assert "ev.stopPropagation()" in body


def test_the_dom_handlers_actually_pass_the_event_through() -> None:
    # A fix in the function body is dead unless the JSX wiring forwards the
    # real event - passing tab alone (dropping ev) would silently defeat it.
    assert re.search(
        r'onClick=\{function \(ev\) \{ selectTab\(tab, ev\); \}\}', SRC
    ), "tab onClick must forward ev to selectTab"
    assert re.search(
        r'onDoubleClick=\{function \(ev\) \{ promoteTab\(tab, ev\); \}\}', SRC
    ), "tab onDoubleClick must forward ev to promoteTab"


def test_bundle_transpiles_with_the_fix() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(ROOT / "ui")
    assert etag and body


# ---------------------------------------------------------------------------
# F7 (US-013): within-group tab reorder. Dropping ON THE TAB BAR used to
# always append (TG_moveTab(..., null)); it must now derive an insertion
# index from the drop X against the OTHER tabs' rendered midpoints.
# ---------------------------------------------------------------------------


def test_tabbar_drop_is_wired_to_the_position_aware_handler() -> None:
    m = re.search(
        r'className="nv-tg-tabbar"\s*\n\s*onDragOver=\{onDragOverIfDragging\}\s*\n\s*'
        r'onDrop=\{function \(ev\) \{ (\w+)\(g\.id, ev\); \}\}',
        SRC,
    )
    assert m, "tab bar's onDrop not found"
    assert m.group(1) == "onTabbarDrop"


def test_move_here_overlay_zone_still_appends_unconditionally() -> None:
    # The "move here" DOCUMENT-BODY zone is a coarser action (drop
    # anywhere in the group's content, not onto a specific tab) - it has
    # no useful position concept and must keep passing null.
    body = _fn_body("onDropMoveHere")
    assert "TG_moveTab(model, dragging, groupId, null)" in body


def test_each_tab_carries_a_data_tab_id_for_the_drop_math() -> None:
    assert "data-tab-id={tab.id}" in SRC


def test_tabbar_drop_excludes_the_dragged_tab_and_computes_a_real_position() -> None:
    body = _fn_body("onTabbarDrop")
    assert 'getAttribute("data-tab-id") === dragging' in body
    assert "continue" in body
    assert "NV_TG_dropIndex(midpoints, ev.clientX)" in body
    assert "TG_moveTab(model, dragging, groupId, position)" in body
    # Must NOT regress to the old always-append call.
    assert "TG_moveTab(model, dragging, groupId, null)" not in body


def test_drop_index_math_is_pure_and_correct() -> None:
    """MiniRacer-executed, not grepped: extracts just NV_TG_dropIndex (the
    rest of the file is JSX, which MiniRacer cannot parse raw) and drives
    it with real numbers."""
    from py_mini_racer import MiniRacer

    start = SRC.index("function NV_TG_dropIndex(")
    end = SRC.index("\n}\n", start) + 2
    fn_src = SRC[start:end]

    ctx = MiniRacer()
    ctx.eval(fn_src)

    midpoints = [50, 150, 250]
    # Before every midpoint -> index 0 (lands first).
    assert ctx.eval(f"NV_TG_dropIndex({midpoints}, 10)") == 0
    # Between the 1st and 2nd midpoint -> index 1.
    assert ctx.eval(f"NV_TG_dropIndex({midpoints}, 100)") == 1
    # Between the 2nd and 3rd midpoint -> index 2.
    assert ctx.eval(f"NV_TG_dropIndex({midpoints}, 200)") == 2
    # Past every midpoint -> index 3 (lands last).
    assert ctx.eval(f"NV_TG_dropIndex({midpoints}, 300)") == 3
    # No other tabs (a lone tab dragged onto itself, or a single-tab
    # group) -> always index 0.
    assert ctx.eval("NV_TG_dropIndex([], 999)") == 0
    # Exactly ON a midpoint stays put (only strictly-past midpoints count).
    assert ctx.eval(f"NV_TG_dropIndex({midpoints}, 150)") == 1


# ---------------------------------------------------------------------------
# F1 (2026-08-29 UI review): session tabs showed the raw sid and a fixed
# accent diamond. Label = session name (id fallback for an unresolved sid),
# glyph = the bound agent/graph's own identity via NV_identity(binding).
# ---------------------------------------------------------------------------


def test_tab_label_takes_a_meta_param_and_falls_back_to_ref() -> None:
    start = SRC.index("function NV_TG_tabLabel(")
    end = SRC.index("\n}\n", start) + 2
    body = SRC[start:end]
    assert "meta" in SRC[start:SRC.index(")", start)], (
        "NV_TG_tabLabel must accept meta alongside tab"
    )
    assert "(meta && meta.name) || tab.ref" in body


def test_kind_glyph_resolves_session_identity_not_a_fixed_diamond() -> None:
    start = SRC.index("function NV_TG_KindGlyph(")
    end = SRC.index("\n}\n", start) + 2
    body = SRC[start:end]
    assert "NV_identity(props.binding)" in body
    # The old unconditional fixed-diamond path for every session tab must
    # be gone, not left as a second, now-dead branch.
    assert body.count('kind === "session"') == 1


def test_session_tab_resolves_meta_once_for_glyph_pulse_and_label() -> None:
    assert "NV_identity" in SRC.splitlines()[0], (
        "the /* global */ directive must declare NV_identity"
    )
    m = re.search(
        r"var meta = tab\.kind === \"session\"[\s\S]{0,200}", SRC,
    )
    assert m
    assert "props.resolveSessionMeta(tab.ref)" in m.group(0)
    assert (
        '<NV_TG_KindGlyph kind={tab.kind} binding={meta && meta.binding} />'
        in SRC
    )
    assert (
        '<NV_TG_SessionPulse sid={tab.ref} wid={meta && meta.wid} />' in SRC
    )
    assert "NV_TG_tabLabel(tab, meta)" in SRC
