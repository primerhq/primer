"""PERF-T10 (M9) perf: listing my SSO identities resolves the provider names in one query, not one ``get`` per identity.

The route's answer is pinned by ``tests/api/test_sso_flow.py`` (only the caller's identities; a deleted
provider tolerated with the raw id as display name; the row shape) and stays green through the change.
This perf half is RED on the N+1 (one ``provider_storage.get`` per identity row) and green once the
provider names come from one ``id IN`` find.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tests.api.conftest import raw_client as client, app  # noqa: F401
from tests.api.test_sso_flow import _login_as
from tests.perf.counting import CountingStorageProvider

from primer.model.oidc import OidcProvider, UserIdentity


async def test_listing_identities_resolves_the_provider_names_in_one_query(client, app):
    sp = app.state.storage_provider
    erin = await _login_as(client, app, user_id="user-erin-perf", username="erin-perf")
    provider = await sp.get_storage(OidcProvider).create(
        OidcProvider(
            name="Test IdP",
            discovery_url="https://idp-perf.example.com/.well-known/openid-configuration",
            client_id="client-perf",
        )
    )
    now = datetime.now(timezone.utc)
    identities = sp.get_storage(UserIdentity)
    ids = [
        await identities.create(UserIdentity(user_id=erin.id, provider_id=provider.id, subject=f"sub-{i}", created_at=now))
        for i in range(3)
    ]
    # Two identities that point at providers since deleted: the list must still answer them.
    ids += [
        await identities.create(UserIdentity(user_id=erin.id, provider_id=f"oidc-gone-{i}", subject=f"sub-x-{i}", created_at=now))
        for i in range(2)
    ]

    csp = CountingStorageProvider(sp)
    app.state.storage_provider = csp
    try:
        with csp.counted() as c:
            r = await client.get("/v1/auth/sso/identities")
    finally:
        app.state.storage_provider = sp

    assert r.status_code == 200, r.text
    body = r.json()
    assert {i["id"] for i in body} == {row.id for row in ids}
    assert next(i for i in body if i["id"] == ids[0].id)["provider_name"] == "Test IdP"
    assert next(i for i in body if i["provider_id"] == "oidc-gone-0")["provider_name"] == "oidc-gone-0"
    # One IN find for the three distinct provider ids, not one get per identity row.
    assert c.calls("find", "get", model="OidcProvider") <= 2
