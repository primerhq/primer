"""Landmarks, a page heading, live regions and names (console review 2026-10-08, C-029).

The behaviour a screen reader meets is checked in a real DOM by ``tests/ui_e2e/test_console_landmarks_journey.py``; these pins are for the
parts a journey cannot reach without a running turn or a pending approval (the status strip, the decision card, a failed turn's card).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
SHELL = (CONSOLE / "nv-shell.jsx").read_text(encoding="utf-8")
CHROME = (CONSOLE / "nv-chrome.jsx").read_text(encoding="utf-8")
STUDIO = (CONSOLE / "nv-studio.jsx").read_text(encoding="utf-8")
FILES = (CONSOLE / "nv-files-sidebar.jsx").read_text(encoding="utf-8")
DOC = (CONSOLE / "nv-session-doc.jsx").read_text(encoding="utf-8")
PLATFORM = (CONSOLE / "nv-platform.jsx").read_text(encoding="utf-8")
MOBILE = (CONSOLE / "nv-mobile-shell.jsx").read_text(encoding="utf-8")
STYLES = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")


def test_the_regions_of_the_shell_are_landmarks() -> None:
    assert 'className="nv-actbar" data-testid="nv-actbar" role="navigation" aria-label="Views"' in CHROME
    assert 'className="nv-topbar" data-testid="nv-topbar" role="banner"' in CHROME
    assert 'className="nv-rail" data-testid="nv-rail" role="navigation" aria-label="Sessions and workspaces"' in STUDIO
    assert 'role="complementary" aria-label="Files"' in FILES


def test_the_view_is_the_main_landmark_and_names_itself_with_a_heading() -> None:
    view = SHELL[SHELL.index('<div className="nv-view"'):]
    view = view[:view.index("</div>\n            </div>")]
    assert 'role="main"' in view[:200]
    assert '<h1 className="nv-sr-only"' in view[:400]
    assert ".nv-sr-only" in STYLES


def test_the_status_strip_and_the_reconnect_note_are_status_regions() -> None:
    assert 'className="nv-status-strip" data-testid="nv-status-strip" role="status"' in DOC
    assert 'className="nv-status-strip" data-testid="nv-reconnect" role="status"' in DOC


def test_a_pending_approval_and_a_failed_turn_interrupt() -> None:
    assert 'data-kind="approval"' in DOC
    card = DOC[DOC.index('<div className="nv-card nv-card-attention" data-kind="approval"'):][:260]
    assert 'role="alert"' in card
    failed = DOC[DOC.index('<div key={row.seq} className="nv-turn-error"'):][:260]
    assert 'role="alert"' in failed


def test_the_composer_is_named_by_what_it_currently_does() -> None:
    composer = DOC[DOC.index('data-testid="nv-composer-input"'):][:700]
    assert "aria-label={composerHint}" in composer and "placeholder={composerHint}" in composer
    assert "var composerHint =" in DOC


def test_the_platform_filters_have_names() -> None:
    assert 'aria-label={"Filter " + (page.title || "list").toLowerCase()}' in PLATFORM
    assert 'aria-label={"Filter " + page.title.toLowerCase()}' in MOBILE


def test_the_phone_shell_is_the_main_landmark() -> None:
    assert 'className="nv-mobile-shell" data-testid="nv-mobile-shell" role="main"' in MOBILE
