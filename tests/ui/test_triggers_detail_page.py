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


def test_fire_now_renders_every_subscriptions_result_inline() -> None:
    """notes 3.6: 'Fire now runs immediately and shows per-subscription
    results inline.' POST .../fire_now is synchronous and already returns
    one result dict per subscription (primer/trigger/dispatch.py); this
    used to be thrown away down to a bare count."""
    src = _src()
    assert 'data-testid="fire-now-results-list"' in src
    assert "fireResult.results.map(" in src


def test_fire_now_result_row_distinguishes_failed_skipped_delivered() -> None:
    src = _src()
    assert '"failed"' in src
    assert '"skipped"' in src
    assert '"delivered"' in src


def test_fire_now_result_row_surfaces_the_error_message() -> None:
    # error_message is the human-readable half of SubscriptionDispatchResult
    # (primer/trigger/subscribers/__init__.py) -- a failed/skipped row is
    # useless without it.
    src = _src()
    assert "r.error_message" in src
    assert "r.artefact_id" in src
