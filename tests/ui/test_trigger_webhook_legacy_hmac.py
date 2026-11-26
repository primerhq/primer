"""Lane J carry: a webhook trigger's legacy body-only signature is shown, switchable, and documented.

``legacy_body_hmac``: while True the trigger also accepts ``HMAC-SHA256(secret, raw_body)`` -
no timestamp, so a captured request replays indefinitely. The console must say so on the
trigger's surface, let the edit dialog switch it (sending the flag explicitly - naming the
flag is the operator's switch per the service), and tell senders the timestamped scheme in
the create wizard.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")
SERVICE = (ROOT / "primer" / "trigger" / "service.py").read_text(encoding="utf-8")


def _body(start: str, end: str) -> str:
    return SRC[SRC.index(start):SRC.index(end, SRC.index(start))]


def test_the_detail_page_badges_a_trigger_that_accepts_body_only_signatures() -> None:
    detail = _body("function TR_TriggerDetail(", "\n}\n")
    assert 'data-testid="webhook-legacy-hmac-badge"' in detail
    assert "Body-only signatures accepted (replayable)" in detail
    # The flag only matters with a secret to sign with (the edit help says so), so the
    # badge condition needs both: a configured secret (hasHmac) and the flag effectively
    # on (the model default is True and a stored row without the field loads True, so
    # absent = on). The three cases it encodes: no secret -> no badge; secret + on ->
    # badge; secret + off -> no badge.
    assert "const hasHmac = !!t.config?.hmac_secret;" in detail
    assert "hasHmac && t.config.legacy_body_hmac !== false &&" in detail


def test_the_edit_dialog_switch_is_seeded_and_sent_explicitly() -> None:
    dialog = _body("function TR_TriggerEditDialog({ trigger, onClose, onSaved })",
                   "\nfunction TR_")
    assert 'id="tr-edit-legacy-hmac"' in dialog
    assert "cfg.legacy_body_hmac !== false" in dialog, "seeded from the row (absent means on)"
    assert "legacy_body_hmac: whLegacyHmac" in dialog, "sent explicitly on the PUT"


def test_the_hmac_secret_writes_carry_the_stored_flag() -> None:
    # Both secret writes rebuild the webhook config whole; an omitted flag would leave the
    # write dependent on the service's keep-stored-when-omitted default.
    assert "legacy_body_hmac: t.config.legacy_body_hmac !== false" in SRC
    assert "legacy_body_hmac: cfg.legacy_body_hmac !== false" in SRC


def test_the_create_wizard_tells_senders_the_timestamped_scheme() -> None:
    step = SRC[SRC.index('data-testid="tr-step-webhook"'):]
    step = step[:step.index("Interactive")]
    assert "X-Primer-Timestamp" in step
    assert "X-Primer-Signature" in step
    # The signed message, exactly as the model documents it: <timestamp>\n<body>.
    assert "<timestamp>" in step and "<body>" in step
    assert "\\\\n" in step, "the wizard must show the literal backslash-n separator"


def test_the_server_contract_the_ui_relies_on() -> None:
    """An update that omits the flag keeps the stored one; naming it is the operator's
    switch; a created trigger that omits it defaults off. The UI pins both directions."""
    assert 'if "legacy_body_hmac" not in config.model_fields_set:' in SERVICE
    assert 'update["legacy_body_hmac"] = False' in SERVICE
    assert 'config.model_copy(update={"legacy_body_hmac": trigger.config.legacy_body_hmac})' in SERVICE
