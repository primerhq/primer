"""REST: a full grantee cannot choose a webhook trigger's token or HMAC secret (access-control P3 SEC, finding 3); the owner can."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.trigger import Trigger, WebhookTriggerConfig
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.conftest import fake_storage_provider  # noqa: F401
from tests.trigger.test_channel_trigger_access import _login, _register_admin, _seed_user

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc)


async def _world(client, app) -> tuple[str, str]:
    await _register_admin(client)
    alice = await _seed_user(app, "alice")
    carol = await _seed_user(app, "carol")
    aref = PrincipalRef(type="user", id=alice, display="alice", role="user", source="local", user_id=alice)
    await app.state.storage_provider.get_storage(Trigger).create(Trigger(
        id="tr-wh", slug="wh", name="w", created_at=NOW, owner=aref, config=WebhookTriggerConfig(token="a" * 32),
        access=Access(owners=[alice], grants={carol: "full"}),
    ))
    return alice, carol


async def test_a_full_grantee_cannot_put_a_token_or_an_hmac_secret(client, app) -> None:
    await _world(client, app)
    await _login(client, "carol")

    chosen = await client.put("/v1/triggers/tr-wh", json={"config": {"kind": "webhook", "token": "c" * 32, "hmac_secret": None}})
    secret = await client.put("/v1/triggers/tr-wh", json={"config": {"kind": "webhook", "token": "a" * 32, "hmac_secret": "s3cret-value"}})
    rotate = await client.post("/v1/triggers/tr-wh/rotate_token")

    assert (chosen.status_code, secret.status_code, rotate.status_code) == (403, 403, 403), (chosen.text, secret.text)
    row = await app.state.storage_provider.get_storage(Trigger).get("tr-wh")
    assert row.config.token == "a" * 32 and row.config.hmac_secret is None


async def test_the_owner_puts_a_token(client, app) -> None:
    await _world(client, app)
    await _login(client, "alice")

    put = await client.put("/v1/triggers/tr-wh", json={"config": {"kind": "webhook", "token": "d" * 32, "hmac_secret": None}})

    assert put.status_code == 200, put.text
    assert (await app.state.storage_provider.get_storage(Trigger).get("tr-wh")).config.token == "d" * 32
