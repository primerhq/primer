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


def test_the_overlay_host_renders_the_triggers_page():
    """The console has no route table: the overlay host IS the wiring."""
    src = (Path(__file__).resolve().parents[2] / "ui" / "components"
           / "shell" / "sh-overlay-host.jsx").read_text()
    assert "TR_TriggersPage" in src


def test_window_export():
    assert "window.TR_TriggersPage" in TRIGGERS.read_text()
