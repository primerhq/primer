"""Static JSX checks for the create-trigger dialog (Phase 9.2)."""

from pathlib import Path

TRIGGERS = Path(__file__).resolve().parents[2] / "ui" / "components" / "triggers.jsx"


def test_create_dialog_defined():
    src = TRIGGERS.read_text()
    assert "TR_CreateTriggerDialog" in src or "TR_CreateDialog" in src


def test_create_dialog_has_kind_picker():
    src = TRIGGERS.read_text()
    assert '"delayed"' in src
    assert '"scheduled"' in src


def test_create_dialog_posts_v1_triggers():
    src = TRIGGERS.read_text()
    assert "/v1/triggers" in src or "POST" in src


def test_create_dialog_has_cron_field():
    src = TRIGGERS.read_text()
    assert "cron" in src.lower()


def test_create_dialog_has_timezone_field():
    src = TRIGGERS.read_text()
    assert "timezone" in src.lower() or "timeZone" in src
