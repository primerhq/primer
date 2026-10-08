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


def test_subscription_row_reads_the_fields_the_backend_now_populates() -> None:
    """01a08bfb item 2: the table's "last fired" / error cells render
    sub.last_fired_at and sub.last_fire_error, which fire_trigger now
    writes per subscription (tests/api/test_triggers_router.py proves the
    REST payload carries them). Pin the render sites so a rename on either
    side cannot silently re-empty the column."""
    src = _src()
    assert "TR_relTime(sub.last_fired_at)" in src
    assert "sub.last_fire_error ?" in src
    assert "error={sub.last_fire_error}" in src
    assert "`sub-row-${sub.id}-error`" in src


def test_fire_error_chip_decodes_the_json_encoded_string_the_backend_stores() -> None:
    """last_fire_error is a JSON-ENCODED STRING on Trigger and Subscription
    rows. The chip used to treat any string as prose and printed the raw
    JSON text as its message."""
    src = _src()
    chip = src[src.index("function TR_FireErrorChip"):]
    chip = chip[: chip.index("\n}\n")]
    assert "JSON.parse(error)" in chip
    # Falls back to the plain-string path when the string is not JSON.
    assert "plain-string message" in chip


def test_a_masked_webhook_token_is_not_rendered_as_a_url() -> None:
    """The server masks a webhook token for anyone but the trigger's owner or an admin (A-20 round 2). The page must not
    build a copyable URL out of the mask; it says who can see the URL instead."""
    src = _src()
    assert 'const TR_TOKEN_MASK = "•••redacted•••";' in src
    url_fn = src[src.index("function TR_webhookUrl"):]
    url_fn = url_fn[: url_fn.index("\n}\n")]
    assert "TR_tokenMasked(trigger)" in url_fn
    assert "visible only to the trigger's owner or an admin" in src


def test_detail_shows_the_owner_and_what_an_ownerless_trigger_fires_as() -> None:
    """Security review A-20: a fired run ranks no higher than the trigger's
    owner, and a trigger saved before owners were recorded fires as an
    ordinary user. The detail page names the owner, or says so."""
    src = _src()
    assert 'data-testid="trigger-owner"' in src
    assert "t.owner.display" in src
    assert "fires as an ordinary user" in src
