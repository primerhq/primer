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
    body = b'{"a": 1}'
    good = _sig("s3cret", body)
    assert webhooks._verify_hmac("s3cret", body, good) is True
    assert webhooks._verify_hmac("s3cret", body, good.removeprefix("sha256=")) is True
    assert webhooks._verify_hmac("s3cret", body, _sig("other", body)) is False
    assert webhooks._verify_hmac("s3cret", body, None) is False
    assert webhooks._verify_hmac("s3cret", body, "") is False


# --------------------------------------------------------------------------- W46


def test_W46_a_non_ascii_signature_is_a_mismatch():
    assert webhooks._verify_hmac("s3cret", b"{}", "sha256=" + "\xff" * 4) is False
    assert webhooks._verify_hmac("s3cret", b"{}", "\ud800") is False


async def test_W46_a_non_ascii_signature_header_answers_401(client, fake_storage_provider):
    from pydantic import SecretStr

    trigger = await create_trigger(
        slug="wh-w46", name="WH", description=None,
        config=WebhookTriggerConfig(hmac_secret=SecretStr("s3cret")), enabled=True,
        deps=ServiceDeps(storage_provider=fake_storage_provider),
    )
    r = await client.post(
        f"/v1/webhooks/{trigger.config.token}", content=b"{}",
        headers={"x-primer-signature": b"sha256=\xff\xff\xff"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["extensions"]["code"] == "hmac_mismatch"
