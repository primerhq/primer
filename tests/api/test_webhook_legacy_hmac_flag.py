"""Review 2026-11-23 6.11, part 2: the per-trigger ``legacy_body_hmac`` flag.

A stored webhook row that predates the flag keeps the body-only signature it
was deployed with (the field loads ``True``); a trigger created from now on
requires the timestamped signature (the create path stores ``False``) unless
the creator set the flag explicitly; an update never flips it behind the
caller's back (an explicit value is the operator's switch).
"""

from __future__ import annotations

import hashlib
import hmac

from pydantic import SecretStr

from primer.model.trigger import Trigger, WebhookTriggerConfig
from primer.trigger.service import ServiceDeps, create_trigger, update_trigger

_SECRET = "s3cret-flag"
_BODY = b'{"event": "flag"}'
_TOKEN = "f" * 32


def _body_only_sig() -> str:
    return "sha256=" + hmac.new(_SECRET.encode(), _BODY, hashlib.sha256).hexdigest()


async def _post_body_only(client, token: str):
    return await client.post(
        f"/v1/webhooks/{token}", content=_BODY, headers={"x-primer-signature": _body_only_sig()},
    )


async def _created(fake_storage_provider, slug: str, **cfg) -> Trigger:
    return await create_trigger(
        slug=slug, name="WH", description=None,
        config=WebhookTriggerConfig(hmac_secret=SecretStr(_SECRET), **cfg), enabled=True,
        deps=ServiceDeps(storage_provider=fake_storage_provider),
    )


async def test_a_stored_row_without_the_field_loads_true_and_accepts_body_only(client, fake_storage_provider):
    cfg = WebhookTriggerConfig.model_validate(
        {"kind": "webhook", "token": _TOKEN, "hmac_secret": _SECRET},
    )
    assert cfg.legacy_body_hmac is True
    from primer.trigger.service import _now

    await fake_storage_provider.get_storage(Trigger).create(Trigger(
        id="tr-legacy-row", slug="wh-legacy-row", name="WH", description=None, config=cfg,
        enabled=True, next_fire_at=None, created_at=_now(),
    ))
    r = await _post_body_only(client, _TOKEN)
    assert r.status_code == 202, r.text


async def test_a_trigger_created_through_the_service_refuses_body_only(client, fake_storage_provider):
    trigger = await _created(fake_storage_provider, "wh-flag-new")
    assert trigger.config.legacy_body_hmac is False
    r = await _post_body_only(client, trigger.config.token)
    assert r.status_code == 401, r.text
    assert r.json()["extensions"]["code"] == "timestamp_required"


async def test_a_trigger_created_through_the_rest_route_refuses_body_only(client, fake_storage_provider):
    r = await client.post("/v1/triggers", json={
        "slug": "wh-flag-rest", "name": "WH",
        "config": {"kind": "webhook", "hmac_secret": _SECRET},
    })
    assert r.status_code == 201, r.text
    stored = await fake_storage_provider.get_storage(Trigger).get(r.json()["id"])
    assert stored.config.legacy_body_hmac is False
    refused = await _post_body_only(client, stored.config.token)
    assert refused.status_code == 401, refused.text
    assert refused.json()["extensions"]["code"] == "timestamp_required"


async def test_an_explicit_true_on_create_is_honoured(client, fake_storage_provider):
    trigger = await _created(fake_storage_provider, "wh-flag-explicit", legacy_body_hmac=True)
    assert trigger.config.legacy_body_hmac is True
    r = await _post_body_only(client, trigger.config.token)
    assert r.status_code == 202, r.text


async def test_an_explicit_true_through_the_rest_route_is_honoured(client, fake_storage_provider):
    r = await client.post("/v1/triggers", json={
        "slug": "wh-flag-rest-true", "name": "WH",
        "config": {"kind": "webhook", "hmac_secret": _SECRET, "legacy_body_hmac": True},
    })
    assert r.status_code == 201, r.text
    stored = await fake_storage_provider.get_storage(Trigger).get(r.json()["id"])
    assert stored.config.legacy_body_hmac is True


async def test_an_update_that_omits_the_flag_keeps_the_stored_value(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    new = await _created(fake_storage_provider, "wh-flag-upd-new")
    kept = await update_trigger(
        trigger_id=new.id, config=WebhookTriggerConfig(hmac_secret=SecretStr("rotated")), deps=deps,
    )
    assert kept.config.legacy_body_hmac is False

    legacy = await _created(fake_storage_provider, "wh-flag-upd-legacy", legacy_body_hmac=True)
    kept = await update_trigger(
        trigger_id=legacy.id, config=WebhookTriggerConfig(hmac_secret=SecretStr("rotated")), deps=deps,
    )
    assert kept.config.legacy_body_hmac is True


async def test_an_update_that_sets_the_flag_explicitly_flips_it_either_way(fake_storage_provider):
    deps = ServiceDeps(storage_provider=fake_storage_provider)
    trigger = await _created(fake_storage_provider, "wh-flag-flip")
    flipped = await update_trigger(
        trigger_id=trigger.id,
        config=WebhookTriggerConfig(hmac_secret=SecretStr(_SECRET), legacy_body_hmac=True), deps=deps,
    )
    assert flipped.config.legacy_body_hmac is True
    back = await update_trigger(
        trigger_id=trigger.id,
        config=WebhookTriggerConfig(hmac_secret=SecretStr(_SECRET), legacy_body_hmac=False), deps=deps,
    )
    assert back.config.legacy_body_hmac is False


async def test_a_rest_put_omitting_the_flag_keeps_the_stored_value(client, fake_storage_provider):
    """Round 1 M6: the UI's set-secret dialog PUTs the full config as read,
    omitting legacy_body_hmac. The stored value must survive both ways."""
    # A fresh trigger stores False; the UI body must keep it False.
    r = await client.post("/v1/triggers", json={
        "slug": "wh-put-keep", "name": "WH",
        "config": {"kind": "webhook", "hmac_secret": _SECRET},
    })
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    p = await client.put(f"/v1/triggers/{tid}", json={"config": {
        "kind": "webhook", "token": "", "hmac_secret": "new-secret",
        "interactive": False, "wait_timeout_seconds": 60,
    }})
    assert p.status_code == 200, p.text
    stored = await fake_storage_provider.get_storage(Trigger).get(tid)
    assert stored.config.legacy_body_hmac is False

    # A legacy trigger stores True; the same UI body must keep it True.
    r2 = await client.post("/v1/triggers", json={
        "slug": "wh-put-keep-legacy", "name": "WH",
        "config": {"kind": "webhook", "hmac_secret": _SECRET, "legacy_body_hmac": True},
    })
    assert r2.status_code == 201, r2.text
    tid2 = r2.json()["id"]
    p2 = await client.put(f"/v1/triggers/{tid2}", json={"config": {
        "kind": "webhook", "token": "", "hmac_secret": "new-secret-2",
        "interactive": False, "wait_timeout_seconds": 60,
    }})
    assert p2.status_code == 200, p2.text
    stored2 = await fake_storage_provider.get_storage(Trigger).get(tid2)
    assert stored2.config.legacy_body_hmac is True
