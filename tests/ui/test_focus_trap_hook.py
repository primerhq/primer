"""One focus trap for every dialog the console draws (console review 2026-10-08, C-028).

``Modal`` (every form modal and the confirm and prompt dialogs) had a trap and a restore; the console's own overlays and the phone's bottom
sheet had none, so after opening the Create session overlay focus stayed on the "+" behind the scrim, Tab walked the whole page, and Escape
returned focus to an unrelated button. The trap is now ``useFocusTrap`` (``foundation/focus-trap.js``) and all three use it. What it DOES is
driven in a real browser by ``tests/ui_e2e/test_overlay_focus_journey.py``; these pins are for the wiring that a browser run cannot tell
apart from a hand-rolled copy.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
HOOK = UI / "foundation" / "focus-trap.js"
INDEX = (UI / "index.html").read_text(encoding="utf-8")
SHARED = (UI / "components" / "shared.jsx").read_text(encoding="utf-8")
SHEET = (UI / "components" / "shared" / "bottom-sheet.jsx").read_text(encoding="utf-8")
OVERLAYS = (UI / "components" / "console" / "nv-overlays.jsx").read_text(encoding="utf-8")


def _hook() -> str:
    return HOOK.read_text(encoding="utf-8")


def test_the_hook_remembers_the_opener_traps_tab_and_restores_focus() -> None:
    src = _hook()
    assert "window.primerApi" in src and "useFocusTrap" in src
    assert "openerRef" in src, "remembers the element that opened the dialog"
    assert "focusables" in src, "Tab and Shift+Tab cycle within these"
    assert 'e.key !== "Tab"' in src or 'key !== "Tab"' in src
    assert "defaultPrevented" in src, "a nested dialog's own wrap is not wrapped a second time"
    assert "document.contains(opener)" in src, "focus goes back only to an opener that still exists"


def test_the_hook_is_loaded_before_the_components_that_use_it() -> None:
    assert 'src="foundation/focus-trap.js"' in INDEX
    assert INDEX.index('src="foundation/focus-trap.js"') < INDEX.index('src="components/shared.jsx"')
    assert INDEX.index('src="foundation/focus-trap.js"') < INDEX.index('src="components/shared/bottom-sheet.jsx"')
    assert INDEX.index('src="foundation/focus-trap.js"') < INDEX.index('src="components/console/nv-overlays.jsx"')


def test_modal_the_sheet_and_the_console_overlay_all_use_the_shared_trap() -> None:
    assert "useFocusTrap(dialogRef" in SHARED
    assert "useFocusTrap(sheetRef" in SHEET
    panel = OVERLAYS[OVERLAYS.index("function NV_OverlayPanel"):OVERLAYS.index("// Field primitive")]
    assert "useFocusTrap(panelRef" in panel


def test_the_console_overlay_is_a_labelled_modal_dialog() -> None:
    panel = OVERLAYS[OVERLAYS.index("function NV_OverlayPanel"):OVERLAYS.index("// Field primitive")]
    assert 'role="dialog"' in panel and 'aria-modal="true"' in panel
    assert "aria-labelledby={titleId}" in panel and "id={titleId}" in panel, "named by its own title"


def test_the_sheet_and_the_modal_are_named_by_their_title() -> None:
    assert "aria-label={typeof title" in SHEET
    assert "aria-label={typeof title" in SHARED


def test_the_sheet_does_not_trap_while_it_is_closed() -> None:
    """BottomSheet stays mounted with ``open`` false; the trap must be gated on it, or a closed sheet would steal Tab."""
    assert "useFocusTrap(sheetRef, !!open" in SHEET
