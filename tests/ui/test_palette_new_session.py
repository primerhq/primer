"""FB6 — the ⌘K palette's "New session" action was a no-op: it pushed an info
toast telling the user to use the sidebar "+" instead of creating a session.

The new-session form visibility now lives in studio state (studio.jsx) so both
the sidebar "+" and the palette action open the SAME form. These checks assert
the wiring across the three files.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PALETTE = UI / "components" / "studio-palette.jsx"
STUDIO = UI / "components" / "studio.jsx"
SIDEBAR = UI / "components" / "studio-sidebar.jsx"


def test_palette_new_session_opens_real_flow() -> None:
    src = PALETTE.read_text(encoding="utf-8")
    # The action opens the real form...
    assert "studio.openNewSession()" in src
    # ...and no longer tells the user to go use the sidebar instead.
    assert "Use the + button in the Sessions sidebar" not in src


def test_studio_exposes_new_session_flow() -> None:
    src = STUDIO.read_text(encoding="utf-8")
    assert "openNewSession" in src
    assert "closeNewSession" in src
    assert "newSessionOpen" in src
    # Exposed on the returned studio object for the sidebar + palette to consume.
    assert "openNewSession: openNewSession" in src
    assert "closeNewSession: closeNewSession" in src
    assert "newSessionOpen: newSessionOpen" in src


def test_sidebar_consumes_lifted_new_session_state() -> None:
    src = SIDEBAR.read_text(encoding="utf-8")
    # Sidebar reads the lifted flag instead of local useState.
    assert "studio.newSessionOpen" in src
    assert "studio.openNewSession()" in src
    assert "studio.closeNewSession()" in src
    # The "+" button testid is preserved, and the sidebar renders the unified
    # create form (FD2). The form overlay's data-testid="new-session-form" now
    # lives in the shared component (asserted in test_shared_new_session_form).
    assert 'data-testid="new-session-btn"' in src
    assert "SharedNewSessionForm" in src


def test_bundle_transpiles_with_new_session_wiring() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    etag, body = build_jsx_bundle(UI)
    assert etag and body
