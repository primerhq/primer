"""The webhook HMAC check answers a mismatch, never an error, for any signature header (W46)."""

from __future__ import annotations

import hashlib
import hmac

from primer.api.routers import webhooks
from primer.model.trigger import WebhookTriggerConfig
from primer.trigger.service import ServiceDeps, create_trigger


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signatures_are_accepted_and_wrong_ones_refused():
    # A distinct trigger_id per call: the verifier's replay cache is keyed
    # per trigger, so these are independent requests.
    body = b'{"a": 1}'
    good = _sig("s3cret", body)
    assert webhooks._verify_webhook_signature(
        "s3cret", body, good, None, trigger_id="tr-w46-1", legacy_body_only=True,
    ) == (True, None)
    assert webhooks._verify_webhook_signature(
        "s3cret", body, good.removeprefix("sha256="), None,
        trigger_id="tr-w46-2", legacy_body_only=True,
    ) == (True, None)
    assert webhooks._verify_webhook_signature(
        "s3cret", body, _sig("other", body), None,
        trigger_id="tr-w46-3", legacy_body_only=True,
    ) == (False, "hmac_mismatch")
    assert webhooks._verify_webhook_signature(
        "s3cret", body, None, None, trigger_id="tr-w46-4", legacy_body_only=True,
    ) == (False, "hmac_mismatch")
    assert webhooks._verify_webhook_signature(
        "s3cret", body, "", None, trigger_id="tr-w46-5", legacy_body_only=True,
    ) == (False, "hmac_mismatch")


# --------------------------------------------------------------------------- W46


def test_W46_a_non_ascii_signature_is_a_mismatch():
    assert webhooks._verify_webhook_signature(
        "s3cret", b"{}", "sha256=" + "\xff" * 4, None,
        trigger_id="tr-w46-6", legacy_body_only=True,
    ) == (False, "hmac_mismatch")
    assert webhooks._verify_webhook_signature(
        "s3cret", b"{}", "\ud800", None, trigger_id="tr-w46-7", legacy_body_only=True,
    ) == (False, "hmac_mismatch")


async def test_W46_a_non_ascii_signature_header_answers_401(client, fake_storage_provider):
    from pydantic import SecretStr

    trigger = await create_trigger(
        slug="wh-w46", name="WH", description=None,
        config=WebhookTriggerConfig(hmac_secret=SecretStr("s3cret"), legacy_body_hmac=True), enabled=True,
        deps=ServiceDeps(storage_provider=fake_storage_provider),
    )
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=b"{}",
        headers={"x-primer-signature": b"sha256=\xff\xff\xff"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["extensions"]["code"] == "hmac_mismatch"
