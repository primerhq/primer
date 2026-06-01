"""Static JSX checks for the trigger detail page (Phase 10.1)."""

from pathlib import Path

TRIGGERS = Path(__file__).resolve().parents[2] / "ui" / "components" / "triggers.jsx"


def _src():
    return TRIGGERS.read_text()


def test_detail_component_defined():
    assert "TR_TriggerDetail" in _src()


def test_detail_renders_metadata_panel():
    src = _src()
    assert "trigger-status-panel" in src or "status-panel" in src


def test_detail_renders_subscriptions_table():
    assert "subscriptions-table" in _src()


def test_detail_has_fire_now():
    src = _src()
    assert "fire_now" in src or "Fire now" in src


def test_detail_uses_polling():
    src = _src()
    assert "useResource" in src or "pollMs" in src


def test_detail_has_add_subscription_btn():
    assert "add-subscription-btn" in _src() or "Add subscription" in _src()
