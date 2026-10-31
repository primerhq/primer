"""``GET /agents/{id}/status`` reports an unusable profile as ONE issue and does not walk what it points at (access-control P4 Task 4)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from httpx import ASGITransport

from primer.auth.tokens import sign_session
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.model_profile import ModelProfile
from primer.model.user import User


async def _user_client(app, uid: str) -> httpx.AsyncClient:
    user = await app.state.storage_provider.get_storage(User).create(
        User(id=uid, username=uid, password_hash=None, created_at=datetime.now(timezone.utc), role="user")
    )
    c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    c.cookies.set(app.state.config.auth.cookie_name, sign_session(
        user_id=user.id, username=user.username, secret=app.state.session_secret, src="local", epoch=user.session_epoch))
    return c


async def test_a_reader_who_may_not_use_the_profile_gets_one_issue_and_no_provider_walk(app):
    sp = app.state.storage_provider
    # The profile names a provider that does not exist: an admin's status says so, a reader without the grant must not learn it.
    await sp.get_storage(ModelProfile).create(
        ModelProfile(id="pro-private", description="d", provider_id="ghost-provider", model_name="m", context_length=1000, access=Access())
    )
    await sp.get_storage(Agent).create(
        Agent.model_validate({"id": "ag-1", "description": "d", "model": {"profile_id": "pro-private"}, "access": {"public": True}})
    )
    ada = await _user_client(app, "u-ada")
    async with ada:
        body = (await ada.get("/v1/agents/ag-1/status")).json()

    assert body["ok"] is False
    assert body["issues"] == ["ModelProfile 'pro-private' is not available to you"]


async def test_an_admin_still_sees_the_walk(client, app):
    sp = app.state.storage_provider
    await sp.get_storage(ModelProfile).create(
        ModelProfile(id="pro-private", description="d", provider_id="ghost-provider", model_name="m", context_length=1000, access=Access())
    )
    await sp.get_storage(Agent).create(Agent.model_validate({"id": "ag-1", "description": "d", "model": {"profile_id": "pro-private"}}))

    body = (await client.get("/v1/agents/ag-1/status")).json()

    assert any("ghost-provider" in issue for issue in body["issues"])
