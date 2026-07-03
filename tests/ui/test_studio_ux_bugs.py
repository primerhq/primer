"""Structural guards for the three Studio UX fixes.

  1. Graph run-view canvas backdrop follows the theme (var(--bg) / the live
     design-token palette), never a hardcoded dark hex — so a light-theme
     toggle restyles the canvas instead of leaving it black.
  2. SD_GraphRunView renders NO event stream of its own; the workspace
     Activity rail is the single source for graph_transition events. The run
     view keeps only the canvas, the node/superstep inspector and the header.
  3. app.jsx's global ⌘K handler bails out on Studio routes
     (window.location.hash startsWith "#/workspaces/") so ⌘K opens ONE palette
     in the Studio (the Studio's own), and still opens the global palette on
     non-Studio pages.

Static-source checks only (no React rendering), matching the approach used in
test_studio_live_consolidation.py / test_run_view_g6.py.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CANVAS = UI / "components" / "graph-canvas.jsx"
DETAIL = UI / "components" / "session-detail.jsx"
APP = UI / "app.jsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


# ---------------------------------------------------------------------------
# Bug 1 — graph canvas backdrop is a theme token, not a hardcoded dark hex
# ---------------------------------------------------------------------------

def test_canvas_container_backdrop_uses_theme_var() -> None:
    src = _read(CANVAS)
    # The rendered canvas container paints its backdrop off the theme token,
    # so the CSS cascade restyles it on a dark/light flip for free.
    assert 'data-testid="graph-canvas"' in src
    assert 'background: "var(--bg)"' in src


def test_g6_background_is_palette_driven_not_hardcoded_dark() -> None:
    src = _read(CANVAS)
    # The G6 canvas background is fed from the live token palette (P.bg reads
    # --bg), both at init and on the theme-toggle restyle.
    assert "background: P.bg" in src
    assert "background: P2.bg" in src
    assert 'bg: v("--bg"' in src
    # No hardcoded dark backdrop literal anywhere in the canvas module.
    for dark in ('background: "#000', 'background:"#000', 'background: "#111',
                 'background: "#0d0f12', 'background: "#0b'):
        assert dark not in src, f"canvas backdrop must not hardcode {dark!r}"


def test_canvas_restyles_on_theme_toggle() -> None:
    src = _read(CANVAS)
    # The MutationObserver on <html data-theme> re-reads the palette and
    # re-applies the background so the canvas follows the Studio theme.
    assert "new MutationObserver(applyTheme)" in src
    assert 'attributeFilter: ["data-theme"]' in src


# ---------------------------------------------------------------------------
# Bug 2 — the run view has no event stream of its own
# ---------------------------------------------------------------------------

def _run_view_body() -> str:
    return _slice(_read(DETAIL), "function SD_GraphRunView(", "const SD_NODE_KIND_HINT")


def test_run_view_has_no_own_event_stream() -> None:
    body = _run_view_body()
    # No dedicated event-tail sub-panel and no EventSource opened for it — the
    # shared workspace tap (Activity rail) is the single event stream.
    assert "new EventSource" not in body
    for testid in ('data-testid="graph-events"', 'data-testid="run-events"'):
        assert testid not in _read(DETAIL), f"run view must not render {testid}"


def test_run_view_keeps_canvas_inspector_and_header() -> None:
    body = _run_view_body()
    # The three surfaces we deliberately keep.
    assert "SD_StatusCanvas" in body        # graph canvas
    assert "SD_NodeInspector" in body        # node / superstep inspector
    assert "Run view" in body                # run header
    assert "superstep" in body               # superstep count in the header


# ---------------------------------------------------------------------------
# Bug 3 — global ⌘K bails out on Studio routes
# ---------------------------------------------------------------------------

def _global_cmdk_handler() -> str:
    return _slice(_read(APP), "to open command palette", "const [toasts")


def test_global_cmdk_guards_studio_route() -> None:
    handler = _global_cmdk_handler()
    # The handler inspects the hash and bails out on Studio routes.
    assert "window.location.hash" in handler
    assert '"#/workspaces/"' in handler
    assert "startsWith" in handler


def test_global_cmdk_returns_before_opening_on_studio_route() -> None:
    handler = _global_cmdk_handler()
    guard = handler.index('startsWith("#/workspaces/")')
    open_call = handler.index("setPaletteOpen((open) => !open)")
    # The guard (and its early return) precede the global-palette toggle, so on
    # a Studio route the global palette never opens.
    assert guard < open_call
    guarded = handler[guard:open_call]
    assert "return" in guarded


# ---------------------------------------------------------------------------
# The whole bundle still transpiles with these edits.
# ---------------------------------------------------------------------------

def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === app.jsx === */" in text
    assert "/* === components/graph-canvas.jsx === */" in text
