"""Graph-canvas field-test fixes (fix/studio-ux-batch).

Two bugs, both in the shared G6 renderer (graph-canvas.jsx) that backs BOTH
the graphs editor and the Studio run-view:

  #1 The run-view had no zoom / traverse controls. An overlaid button cluster
     (zoom-in / zoom-out / fit / reset-100%) is now wired to the live G6 v5
     instance via zoomBy / fitView / zoomTo. Shared component => works on both
     surfaces.

  #6 The dark/light toggle did not restyle the canvas because G6 styling used
     hardcoded hex literals. The palette is now read from the CSS design tokens
     via getComputedStyle (same pattern as studio-terminal.jsx's xterm theme)
     and re-applied on a data-theme change via a MutationObserver.

Static source-grep + a bundle-transpile gate, matching the other UI canvas
tests (no browser needed to assert the wiring is present).
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
CANVAS = (UI / "components" / "graph-canvas.jsx").read_text(encoding="utf-8")
STYLES = (UI / "styles.css").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# #1 — zoom / traverse controls
# --------------------------------------------------------------------------

def test_control_testids_present() -> None:
    for tid in ("graph-zoom-in", "graph-zoom-out", "graph-fit", "graph-zoom-reset"):
        assert f'data-testid="{tid}"' in CANVAS, tid
    # the cluster wrapper
    assert 'data-testid="graph-controls"' in CANVAS


def test_controls_are_keyboard_accessible_buttons() -> None:
    # Real <button>s with aria-labels (not clickable divs) => keyboard reachable.
    assert 'aria-label="Zoom in"' in CANVAS
    assert 'aria-label="Zoom out"' in CANVAS
    assert 'aria-label="Fit graph to view"' in CANVAS
    assert 'aria-label="Reset zoom to 100%"' in CANVAS
    assert CANVAS.count('type="button"') >= 4


def test_controls_wired_to_g6_zoom_fit_apis() -> None:
    # G6 v5 camera APIs (verified against the vendored g6.min.js).
    assert "zoomBy(" in CANVAS      # zoom-in / zoom-out (relative ratio)
    assert "fitView(" in CANVAS     # fit-to-view
    assert "zoomTo(" in CANVAS      # reset to 100%
    # wired through the live instance ref, not a fresh graph
    assert "graphRef.current" in CANVAS


def test_controls_have_themeable_styles() -> None:
    assert ".gr-canvas-controls" in STYLES
    assert ".gr-ctrl-btn" in STYLES
    # themed off tokens so they follow dark/light
    assert "var(--border)" in STYLES
    assert "position: absolute" in STYLES


# --------------------------------------------------------------------------
# #6 — theme-token driven palette + restyle-on-toggle
# --------------------------------------------------------------------------

def test_palette_read_from_css_tokens() -> None:
    # Reads the live design tokens instead of hardcoding hex.
    assert "getComputedStyle(document.documentElement)" in CANVAS
    assert "getPropertyValue(" in CANVAS
    for token in ("--green", "--amber", "--red", "--violet", "--bg", "--text", "--border"):
        assert f'"{token}"' in CANVAS, token


def test_hardcoded_g6_palette_removed() -> None:
    # The old hardcoded palette object is gone and no G6 node/edge style still
    # references it — the node/edge/state styles now flow from the token palette.
    assert "_G6_COLORS" not in CANVAS
    assert "const _G6_COLORS" not in CANVAS
    # palette builder + token-driven style builders exist
    assert "function _g6Palette(" in CANVAS
    assert "_g6NodeStyle(P)" in CANVAS
    assert "_g6EdgeStyle(P)" in CANVAS


def test_restyle_observes_data_theme() -> None:
    assert "MutationObserver" in CANVAS
    assert '"data-theme"' in CANVAS
    assert "attributeFilter" in CANVAS
    # re-reads tokens + re-pushes styles on toggle, and is torn down on unmount
    assert "setOptions(" in CANVAS
    assert ".disconnect()" in CANVAS


# --------------------------------------------------------------------------
# bundle still transpiles with the changes
# --------------------------------------------------------------------------

def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    assert "/* === components/graph-canvas.jsx === */" in body.decode("utf-8")
