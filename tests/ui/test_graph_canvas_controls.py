"""Graph-canvas field-test fixes (fix/studio-ux-batch).

Bug #6 — the dark/light toggle did not restyle the shared G6 canvas
(graph-canvas.jsx, behind BOTH the graphs editor and the Studio run-view)
because its styling used hardcoded hex literals. The palette is now read from
the CSS design tokens via getComputedStyle (same pattern as
studio-terminal.jsx's xterm theme) and re-applied on a data-theme change via a
MutationObserver.

Static source-grep + a bundle-transpile gate, matching the other UI canvas
tests (no browser needed to assert the wiring is present).
"""

from __future__ import annotations

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
CANVAS = (UI / "components" / "graph-canvas.jsx").read_text(encoding="utf-8")


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
