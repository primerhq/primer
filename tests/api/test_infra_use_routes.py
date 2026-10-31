"""Use grants on model profiles and providers over REST (access-control P4 Task 3, spec 6.1, 6.2).

``client`` is the suite's admin; ``_user_client`` signs in a plain user. Creating and editing a row stays admin-only; the use grant is
the ``/access`` sub-resource, readable by a grantee and changed only by an admin; the choices lists show a user only the rows they may use.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from httpx import ASGITransport
from pydantic import SecretStr

from primer.auth.tokens import sign_session
from primer.model.access import Access
from primer.model.model_profile import ModelProfile
from primer.model.provider import AnthropicConfig, LLMProvider, LLMProviderType, Limits
from primer.model.user import User
from primer.model.workspace import WorkspaceProvider


async def _user_client(app, uid: str, role: str = "user") -> httpx.AsyncClient:
    user = await app.state.storage_provider.get_storage(User).create(
        User(id=uid, username=uid, password_hash=None, created_at=datetime.now(timezone.utc), role=role)
    )
    c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    c.cookies.set(app.state.config.auth.cookie_name, sign_session(
        user_id=user.id, username=user.username, secret=app.state.session_secret, src="local", epoch=user.session_epoch))
    return c


async def _provider(app, pid: str = "anthropic-1", access: Access | None = None) -> None:
    row = LLMProvider(
        id=pid, provider=LLMProviderType.ANTHROPIC, config=AnthropicConfig(api_key=SecretStr("x")), limits=Limits(max_concurrency=4),
    )
    await app.state.storage_provider.get_storage(LLMProvider).create(row if access is None else row.model_copy(update={"access": access}))


async def _profile(app, pid: str, access: Access | None = None) -> ModelProfile:
    row = ModelProfile(id=pid, description=pid, provider_id="anthropic-1", model_name="m", context_length=8192)
    if access is not None:
        row = row.model_copy(update={"access": access})
    return await app.state.storage_provider.get_storage(ModelProfile).create(row)


async def test_create_is_private_and_ignores_access_in_the_body(client, app):
    await _provider(app)
    body = ModelProfile(id="p-new", description="d", provider_id="anthropic-1", model_name="m", context_length=8192).model_dump(mode="json")
    body["access"] = {"owners": ["u-evil"], "grants": {"u-evil": "read"}, "public": True}

    resp = await client.post("/v1/model_profiles", json=body)

    assert resp.status_code == 201, resp.text
    stored = await app.state.storage_provider.get_storage(ModelProfile).get("p-new")
    assert stored.access == Access()  # private, no owner, nobody granted


async def test_profile_update_keeps_use_grants(client, app):
    await _provider(app)
    await _profile(app, "p-1", Access(grants={"u1": "read"}, public=True, rev=2))
    body = (await client.get("/v1/model_profiles/p-1")).json()
    body["description"] = "renamed"
    body["access"] = {"owners": [], "grants": {}, "public": False}  # a body can never reset the grant

    resp = await client.put("/v1/model_profiles/p-1", json=body)

    assert resp.status_code == 200, resp.text
    stored = await app.state.storage_provider.get_storage(ModelProfile).get("p-1")
    assert stored.description == "renamed"
    assert stored.access.grants == {"u1": "read"} and stored.access.public and stored.access.rev == 2


async def test_llm_provider_update_keeps_use_grants(client, app):
    await _provider(app, access=Access(grants={"u1": "read"}, rev=1))
    body = (await client.get("/v1/llm_providers/anthropic-1")).json()
    body["limits"] = {"max_concurrency": 2}

    resp = await client.put("/v1/llm_providers/anthropic-1", json=body)

    assert resp.status_code == 200, resp.text
    stored = await app.state.storage_provider.get_storage(LLMProvider).get("anthropic-1")
    assert stored.limits.max_concurrency == 2
    assert stored.access.grants == {"u1": "read"} and stored.access.rev == 1


async def test_the_use_grant_is_read_by_a_grantee_and_changed_only_by_an_admin(client, app):
    await _provider(app)
    await _profile(app, "p-1", Access(grants={"u-ada": "read"}, rev=1))
    ada = await _user_client(app, "u-ada")
    bob = await _user_client(app, "u-bob")
    async with ada, bob:
        view = await ada.get("/v1/model_profiles/p-1/access")
        assert view.status_code == 200 and view.json()["your_level"] == "read" and view.json()["owners"] == []
        assert (await bob.get("/v1/model_profiles/p-1/access")).status_code == 404  # exists, but reads as missing
        update = {"owners": [], "grants": {"u-ada": "read", "u-bob": "read"}, "public": False}
        assert (await ada.put("/v1/model_profiles/p-1/access", json=update)).status_code == 403  # a grantee cannot widen it
        assert (await bob.put("/v1/model_profiles/p-1/access", json=update)).status_code == 404

        shared = await client.put("/v1/model_profiles/p-1/access", json={"owners": [], "grants": {"u-ada": "read", "u-bob": "read"}, "public": False, "rev": 1})
        assert shared.status_code == 200, shared.text
        assert (await bob.get("/v1/model_profiles/p-1/access")).status_code == 200


async def test_the_use_grant_refuses_owners_full_grants_and_public_tools(client, app):
    await _provider(app)
    await _profile(app, "p-1")
    for update in (
        {"owners": ["admin-1"]}, {"owners": [], "grants": {"u1": "full"}}, {"owners": [], "public_tools": ["t"]},
    ):
        resp = await client.put("/v1/model_profiles/p-1/access", json=update)
        assert resp.status_code in (400, 404, 422), (update, resp.text)


async def test_choices_list_only_usable_profiles(client, app):
    await _provider(app)
    await _profile(app, "p-private")
    await _profile(app, "p-public", Access(public=True))
    await _profile(app, "p-granted", Access(grants={"u-ada": "read"}))
    ada = await _user_client(app, "u-ada")
    bob = await _user_client(app, "u-bob")
    async with ada, bob:
        ada_ids = [c["id"] for c in (await ada.get("/v1/model_profiles/choices")).json()["items"]]
        bob_ids = [c["id"] for c in (await bob.get("/v1/model_profiles/choices")).json()["items"]]
    admin_ids = [c["id"] for c in (await client.get("/v1/model_profiles/choices")).json()["items"]]
    mine = {"p-private", "p-public", "p-granted"}  # the app fixture seeds a profile of its own too
    assert mine & set(ada_ids) == {"p-granted", "p-public"}
    assert mine & set(bob_ids) == {"p-public"}
    assert mine <= set(admin_ids)


async def test_workspace_provider_choices_only_usable(client, app):
    store = app.state.storage_provider.get_storage(WorkspaceProvider)
    mk = lambda pid, access: WorkspaceProvider.model_validate({"id": pid, "provider": "local", "config": {"kind": "local"}, "access": access.model_dump(mode="json")})  # noqa: E731
    await store.create(mk("wp-public", Access(public=True)))
    await store.create(mk("wp-private", Access()))
    await store.create(mk("wp-granted", Access(grants={"u-ada": "read"})))
    ada = await _user_client(app, "u-ada")
    bob = await _user_client(app, "u-bob")
    async with ada, bob:
        ada_ids = [c["id"] for c in (await ada.get("/v1/workspace_providers/_choices")).json()["items"]]
        bob_ids = [c["id"] for c in (await bob.get("/v1/workspace_providers/_choices")).json()["items"]]
    admin_ids = [c["id"] for c in (await client.get("/v1/workspace_providers/_choices")).json()["items"]]
    assert sorted(ada_ids) == ["wp-granted", "wp-public"] and bob_ids == ["wp-public"]
    assert {"wp-public", "wp-private", "wp-granted"} <= set(admin_ids)
