"""Static JSX checks for the triggers list page (Phase 9.1)."""

from pathlib import Path

TRIGGERS = Path(__file__).resolve().parents[2] / "ui" / "components" / "triggers.jsx"
OVERLAYS = Path(__file__).resolve().parents[2] / "ui" / "foundation" / "shell-url.js"
APP = Path(__file__).resolve().parents[2] / "ui" / "app.jsx"


def test_triggers_page_defined():
    assert "TR_TriggersPage" in TRIGGERS.read_text()


def test_triggers_grid_testid():
    assert 'data-testid="triggers-grid"' in TRIGGERS.read_text()


def test_triggers_is_a_registered_overlay():
    """The shell has no sidebar: every page-shaped surface is an overlay."""
    assert '"triggers"' in OVERLAYS.read_text()


def test_app_routes_triggers():
    src = APP.read_text()
    assert "/triggers" in src or "triggers" in src


def test_window_export():
    assert "window.TR_TriggersPage" in TRIGGERS.read_text()
