"""REST chat-create attribution — Layer 3 Task 5 (spec §8.4).

``POST /v1/chats`` must stamp the created row's ``initiated_by`` from the
authenticated caller (``request.state.actor``, Layer 1's
``AuthMiddleware``), falling back to the reserved system principal when
the request carries no actor. Mirrors
``tests/api/test_session_create_attribution.py`` for the chat surface.
"""

from __future__ import annotations

import pytest

from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401

from primer.model.agent import Agent, AgentModel
from primer.model.chats import Chat
from primer.model.user import User


async def _seed_agent(app) -> Agent:
    sp = app.state.storage_provider
    agent = Agent(
        id="ag-attr-1",
        description="attribution test agent",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    await sp.get_storage(Agent).create(agent)
    return agent


@pytest.mark.asyncio
async def test_create_chat_stamps_user_initiated_by(client, app) -> None:
    """A logged-in user's chat create stamps ``initiated_by`` from them."""
    agent = await _seed_agent(app)

    reg = await client.post(
        "/v1/auth/register",
        json={"username": "attruser", "password": "attrpassword"},
    )
    assert reg.status_code == 200, reg.text

    users = app.state.storage_provider.get_storage(User)
    user_row = next(
        u for u in users._data.values() if u.username == "attruser"  # noqa: SLF001
    )

    resp = await client.post("/v1/chats", json={"agent_id": agent.id})
    assert resp.status_code == 201, resp.text
    chat_id = resp.json()["id"]

    chats = app.state.storage_provider.get_storage(Chat)
    row = await chats.get(chat_id)
    assert row is not None
    assert row.initiated_by is not None
    assert row.initiated_by.type == "user"
    assert row.initiated_by.id == user_row.id
    assert row.initiated_by.source in {"local", "internal"}


@pytest.mark.asyncio
async def test_create_chat_falls_back_to_system_when_unauthenticated(
    client, app,
) -> None:
    """No real actor on the request -> ``initiated_by`` is ``system``.

    With auth *enabled* (the default), an unauthenticated request never
    reaches the handler at all -- ``require_auth`` 401s first. The only
    way a real request reaches ``create_chat`` with no genuine user actor
    is auth-disabled mode, where ``AuthMiddleware`` stamps a synthetic
    system ``Principal`` onto ``request.state.actor`` -- this exercises
    the same ``PrincipalRef`` projection code path the ``actor is None``
    fallback covers.
    """
    app.state.config.auth.enabled = False
    agent = await _seed_agent(app)

    resp = await client.post("/v1/chats", json={"agent_id": agent.id})
    assert resp.status_code == 201, resp.text
    chat_id = resp.json()["id"]

    chats = app.state.storage_provider.get_storage(Chat)
    row = await chats.get(chat_id)
    assert row is not None
    assert row.initiated_by is not None
    assert row.initiated_by.type == "system"
