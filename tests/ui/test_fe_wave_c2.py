"""FE Wave C2 — spacing/type/radius tokens + st-* utility classes (FC2) and
a11y hardening (FC5): Modal focus-trap/restore, --text-4 contrast bump, and
keyboard-accessible Studio sidebar rows."""

from pathlib import Path

_UI = Path(__file__).resolve().parents[2] / "ui"
STYLES = (_UI / "styles.css").read_text()
SHARED = (_UI / "components" / "shared.jsx").read_text()
SIDEBAR = (_UI / "components" / "studio-sidebar.jsx").read_text()
ACTIVITY = (_UI / "components" / "studio-activity.jsx").read_text()


# ---- FC2: tokens + utility classes -----------------------------------------

def test_fc2_spacing_type_radius_tokens_defined() -> None:
    for tok in ("--fs-11:", "--fs-12:", "--fs-13:",
                "--sp-1:", "--sp-2:", "--sp-3:", "--sp-4:",
                "--r-6:", "--r-9:", "--r-12:"):
        assert tok in STYLES, f"missing token {tok}"


def test_fc2_st_utility_classes_defined_and_used() -> None:
    for cls in (".st-row", ".st-section-label", ".st-pill", ".st-panel-bar"):
        assert cls in STYLES, f"missing utility class {cls}"
    # The recurring patterns were adopted in the two targeted files.
    assert "st-section-label" in SIDEBAR or "st-section-label" in ACTIVITY


# ---- FC5a: Modal focus-trap + restore --------------------------------------

def test_fc5_modal_traps_and_restores_focus() -> None:
    assert 'aria-modal="true"' in SHARED
    assert "openerRef" in SHARED          # remembers the opener
    assert "focusables" in SHARED         # trap cycles within these
    assert 'tabIndex={-1}' in SHARED      # dialog is focusable as a fallback


# ---- FC5b: --text-4 contrast bump ------------------------------------------

def test_fc5_text4_contrast_raised() -> None:
    # Both themes moved --text-4 toward AA contrast; the old low-contrast
    # values (0.4 on dark, 0.7 on light) are gone.
    assert "--text-4: oklch(0.4 " not in STYLES
    assert "--text-4: oklch(0.7 " not in STYLES


# ---- FC5c: keyboard-accessible sidebar rows --------------------------------

def test_fc5_sidebar_rows_are_keyboard_accessible() -> None:
    assert "function ST_onRowKey(" in SIDEBAR
    # Both the session row and the file row expose role/tabindex + key handler.
    for testid in ('data-testid="session-row"', 'data-testid="file-row"'):
        assert testid in SIDEBAR
    assert SIDEBAR.count("onKeyDown={ST_onRowKey(") >= 2
    assert SIDEBAR.count('role="button"') >= 2
