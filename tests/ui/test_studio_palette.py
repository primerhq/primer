"""Structural-presence checks for studio-palette.jsx (PR-B / B5).

Verifies:
  - window.CommandPalette and window.QuickOpen are exported
  - required data-testids are present
  - StudioHeader trigger buttons are wired (not no-ops) in studio.jsx
  - the global keydown listener is registered in Studio
  - pushToast is threaded from app.jsx into <Studio>
  - studio-palette.jsx loads before studio.jsx in index.html
  - the full bundle transpiles cleanly

Static-source checks only (no React rendering), matching the approach used
in test_studio_shell.py / test_studio_sidebar.py.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PALETTE = UI / "components" / "studio-palette.jsx"
STUDIO = UI / "components" / "studio.jsx"
APP = UI / "app.jsx"
INDEX = UI / "index.html"


def _palette_src() -> str:
    return PALETTE.read_text(encoding="utf-8")


def _studio_src() -> str:
    return STUDIO.read_text(encoding="utf-8")


def _app_src() -> str:
    return APP.read_text(encoding="utf-8")


def _index_order() -> list[str]:
    out: list[str] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if 'type="text/babel"' in line and "src=" in line:
            start = line.index('src="') + len('src="')
            end = line.index('"', start)
            out.append(line[start:end])
    return out


# ---------------------------------------------------------------------------
# studio-palette.jsx presence + exports
# ---------------------------------------------------------------------------

def test_palette_file_exists() -> None:
    assert PALETTE.exists(), "studio-palette.jsx is missing"


def test_palette_exports_command_palette() -> None:
    src = _palette_src()
    # Renamed to StudioCommandPalette to avoid colliding with chrome.jsx's
    # app-global CommandPalette in the flat, no-IIFE bundle scope (FB1).
    assert "function StudioCommandPalette(" in src
    assert "window.StudioCommandPalette = StudioCommandPalette;" in src
    # The Studio palette must NOT declare/export the app-global name.
    assert "function CommandPalette(" not in src
    assert "window.CommandPalette = CommandPalette;" not in src


def test_palette_exports_quick_open() -> None:
    src = _palette_src()
    assert "function QuickOpen(" in src
    assert "window.QuickOpen = QuickOpen;" in src


# ---------------------------------------------------------------------------
# Required data-testids
# ---------------------------------------------------------------------------

def test_command_palette_testid() -> None:
    src = _palette_src()
    assert 'data-testid="command-palette"' in src


def test_palette_input_testid() -> None:
    src = _palette_src()
    assert 'data-testid="palette-input"' in src


def test_palette_item_testid() -> None:
    src = _palette_src()
    assert 'data-testid="palette-item"' in src


def test_quick_open_testid() -> None:
    src = _palette_src()
    assert 'data-testid="quick-open"' in src


def test_quick_open_input_testid() -> None:
    src = _palette_src()
    assert 'data-testid="quick-open-input"' in src


def test_quick_open_item_testid() -> None:
    src = _palette_src()
    assert 'data-testid="quick-open-item"' in src


# ---------------------------------------------------------------------------
# Fuzzy match helper present
# ---------------------------------------------------------------------------

def test_fuzzy_match_helper_present() -> None:
    src = _palette_src()
    assert "STP_fuzzy" in src


# ---------------------------------------------------------------------------
# StudioHeader trigger buttons wired (not no-ops) in studio.jsx
# ---------------------------------------------------------------------------

def test_header_palette_trigger_wired() -> None:
    src = _studio_src()
    # onTogglePalette must be wired to studio.togglePalette, not a no-op lambda
    assert "onTogglePalette={studio.togglePalette}" in src


def test_header_quickopen_trigger_wired() -> None:
    src = _studio_src()
    # onOpenQuick must call studio.openPalette("quickopen")
    assert 'studio.openPalette("quickopen")' in src


# ---------------------------------------------------------------------------
# Global keydown effect in Studio
# ---------------------------------------------------------------------------

def test_studio_has_keydown_effect() -> None:
    src = _studio_src()
    # The useEffect that registers the keydown listener must be present
    assert "addEventListener" in src and "keydown" in src


def test_keydown_handles_cmd_k() -> None:
    src = _studio_src()
    assert 'e.key === "k"' in src or "e.key === 'k'" in src


def test_keydown_handles_cmd_p() -> None:
    src = _studio_src()
    assert 'e.key === "p"' in src or "e.key === 'p'" in src


def test_keydown_handles_ctrl_backtick() -> None:
    src = _studio_src()
    assert 'e.key === "`"' in src or 'e.key === \'`\'' in src


def test_keydown_handles_escape() -> None:
    src = _studio_src()
    assert 'e.key === "Escape"' in src


# ---------------------------------------------------------------------------
# Palette state in useStudioState + Studio renders the overlays
# ---------------------------------------------------------------------------

def test_studio_state_has_palette_fields() -> None:
    src = _studio_src()
    assert "paletteOpen" in src
    assert "paletteMode" in src


def test_studio_renders_command_palette() -> None:
    src = _studio_src()
    assert "<StudioCommandPalette" in src


def test_studio_renders_quick_open() -> None:
    src = _studio_src()
    assert "<QuickOpen" in src


def test_studio_state_exposes_open_palette() -> None:
    src = _studio_src()
    assert "openPalette" in src
    assert "closePalette" in src
    assert "togglePalette" in src


# ---------------------------------------------------------------------------
# pushToast threaded from app.jsx into Studio
# ---------------------------------------------------------------------------

def test_app_passes_push_toast_to_studio() -> None:
    src = _app_src()
    assert "pushToast={pushToast}" in src
    # Must appear on the Studio render line, not just another component
    assert "<Studio wid={currentWorkspaceId} pushToast={pushToast} />" in src


def test_studio_accepts_push_toast_prop() -> None:
    src = _studio_src()
    # Studio function signature accepts pushToast
    assert "pushToast" in src
    # It is stored on studio object for sub-components to consume
    assert "studio.pushToast" in src


# ---------------------------------------------------------------------------
# Load order in index.html
# ---------------------------------------------------------------------------

def test_palette_registered_before_studio() -> None:
    order = _index_order()
    assert "components/studio-palette.jsx" in order, "studio-palette.jsx not in index.html"
    palette_idx = order.index("components/studio-palette.jsx")
    studio_idx = order.index("components/studio.jsx")
    assert palette_idx < studio_idx, (
        f"studio-palette.jsx (pos {palette_idx}) must come before "
        f"studio.jsx (pos {studio_idx})"
    )


def test_palette_registered_after_studio_activity() -> None:
    order = _index_order()
    activity_idx = order.index("components/studio-activity.jsx")
    palette_idx = order.index("components/studio-palette.jsx")
    assert activity_idx < palette_idx


# ---------------------------------------------------------------------------
# Bundle transpiles cleanly
# ---------------------------------------------------------------------------

def test_bundle_transpiles_with_palette() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio-palette.jsx === */" in text
    assert "/* === components/studio.jsx === */" in text
