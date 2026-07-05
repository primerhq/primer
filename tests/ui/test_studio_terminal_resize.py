"""Structural guards for the Studio terminal vertical-resize fix.

BUG: the bottom TERMINAL panel could not be dragged/resized — there was no
resize handle between StudioCenter and TerminalPanel and the panel height was a
hard-coded CSS constant (`.st-term-panel { flex: 0 0 240px }`) with no backing
state.

FIX: mirror the working left/right column-resize pattern, rotated to the Y
axis. A `terminalHeight` state field (persisted) drives the panel's flex-basis
via a `--st-term-h` CSS var; a `.st-term-resize` divider between the editor and
the terminal runs `startTermResize`, which grows the panel as it is dragged up.

Static-source checks only (no React rendering), matching test_studio_shell.py /
test_studio_ux_bugs.py.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
STUDIO = UI / "components" / "studio.jsx"
STYLES = UI / "styles.css"


def _studio_src() -> str:
    return STUDIO.read_text(encoding="utf-8")


def _styles_src() -> str:
    return STYLES.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# terminalHeight state — default + persisted, mirroring leftWidth/rightWidth.
# ---------------------------------------------------------------------------

def test_terminal_height_state_exists() -> None:
    src = _studio_src()
    # Default state carries a numeric terminalHeight (backs the panel height).
    assert "terminalHeight: 240" in src
    # And it round-trips through localStorage like the column widths.
    assert '"terminalHeight"' in src


def test_set_terminal_height_action_exposed() -> None:
    src = _studio_src()
    # The setter exists alongside setLeftWidth/setRightWidth...
    assert "var setTerminalHeight = React.useCallback(" in src
    assert "{ terminalHeight: h }" in src
    # ...and is surfaced on the store's returned object.
    assert "setTerminalHeight: setTerminalHeight" in src


# ---------------------------------------------------------------------------
# Drag handler + handle element — mirror startResize on the Y axis.
# ---------------------------------------------------------------------------

def test_start_term_resize_handler_present() -> None:
    src = _studio_src()
    assert "function startTermResize(" in src
    # Uses clientY (vertical) and inverts the delta so dragging UP grows the
    # panel (the terminal sits below the content).
    assert "e.clientY" in src
    assert "d.startY - ev.clientY" in src
    # Writes the clamped height through the exposed setter.
    assert "studio.setTerminalHeight(" in src
    # Tears down its window listeners on mouseup, like startResize.
    assert 'window.removeEventListener("mousemove", onMove)' in src
    assert 'window.removeEventListener("mouseup", onUp)' in src


def test_terminal_resize_handle_rendered_between_center_and_panel() -> None:
    src = _studio_src()
    # The divider is gated on terminalOpen and wired to the drag handler.
    assert 'data-testid="terminal-resize"' in src
    assert 'className="st-term-resize"' in src
    assert "onMouseDown={startTermResize}" in src
    # It must sit BEFORE the TerminalPanel mount inside the center column.
    handle = src.index('data-testid="terminal-resize"')
    panel = src.index("<TerminalPanel wid={wid}")
    assert handle < panel, "resize handle must precede the terminal panel"


# ---------------------------------------------------------------------------
# Height is state-driven via a CSS var, not a hard-coded constant.
# ---------------------------------------------------------------------------

def test_term_height_css_var_wired_on_root() -> None:
    src = _studio_src()
    # The var is written inline on .st-root next to --st-left-w / --st-right-w.
    assert '"--st-term-h": s.terminalHeight + "px"' in src


def test_term_panel_flex_reads_css_var() -> None:
    css = _styles_src()
    # The panel's flex-basis reads the var (240px fallback), not a fixed 240px.
    assert "flex: 0 0 var(--st-term-h, 240px);" in css
    assert "flex: 0 0 240px;" not in css  # old hard-coded value is gone


def test_term_resize_handle_styled() -> None:
    css = _styles_src()
    assert ".st-term-resize {" in css
    assert "cursor: row-resize;" in css
    # A thin horizontal hit-area, mirroring .st-resize.
    assert "height: 5px;" in css


# ---------------------------------------------------------------------------
# The whole bundle still transpiles with these edits.
# ---------------------------------------------------------------------------

def test_bundle_transpiles() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
    text = body.decode("utf-8")
    assert "/* === components/studio.jsx === */" in text
