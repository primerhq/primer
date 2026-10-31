"""The acting person (access-control plan P5 Task 1): who the current request, listing or tool call acts for.

The auth middleware sets it for every authenticated request (the user; a bearer token's owner, never the token; the system person when auth is
disabled) and resets it when the request ends. ``person_for_tool`` with no ``ToolContext`` reads it first, then the MCP request's actor.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import Request

from primer.access.acting import acting_as, acting_person, actor_label
from primer.access.person import Person, person_for_tool
from primer.auth.api_tokens import extract_prefix, hash_token, mint_plaintext
from primer.mcp.server import current_actor
from primer.model.api_token import ApiToken
from primer.model.principal import Principal, PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.model.yield_ import ToolContext

A = Person("u-a", "user")
B = Person("u-b", "admin")


def test_acting_as_sets_and_resets():
    assert acting_person() is None
    with acting_as(A):
        assert acting_person() == A
        with acting_as(B):
            assert acting_person() == B
        assert acting_person() == A
    assert acting_person() is None
    with pytest.raises(RuntimeError), acting_as(A):
        raise RuntimeError("the block failed")
    assert acting_person() is None, "the person is reset whatever happens inside"


def test_actor_label():
    assert actor_label(A) == "user:u-a"
    assert actor_label(Person(None, "system")) == "system"
    assert actor_label(Person(None, "user")) == "system"
    assert actor_label(None) == "system"


def test_person_for_tool_reads_the_acting_person_when_there_is_no_ctx():
    mcp = Principal(type="api_token", id="at-1", display="t", role="user", source="internal", user_id="u-mcp")
    assert person_for_tool(None) == Person(None, "user")
    reset = current_actor.set(mcp)
    try:
        assert person_for_tool(None) == Person("u-mcp", "user")
        with acting_as(A):
            assert person_for_tool(None) == A, "the acting person comes before the MCP actor"
            run = ToolContext(tool_call_id="t", session_id="s", workspace_id=None,
                              initiated_by=PrincipalRef(type="user", id="u-run", display="r", role="user", source="local", user_id="u-run"))
            assert person_for_tool(run) == Person("u-run", "user"), "a run's ctx wins over whoever is acting"
    finally:
        current_actor.reset(reset)


def _mount_probe(app) -> None:
    async def probe(request: Request) -> dict:
        person = acting_person()
        return {"label": actor_label(person) if person is not None else "nobody", "role": person.role if person is not None else None}

    app.add_api_route("/v1/_test_acting_person", probe, methods=["GET"])


async def _user(fake_storage_provider) -> User:
    return (await fake_storage_provider.get_storage(User).list(OffsetPage(offset=0, length=10))).items[0]


async def test_middleware_resets_after_the_request(app, client, fake_storage_provider):
    _mount_probe(app)
    user = await _user(fake_storage_provider)

    answer = (await client.get("/v1/_test_acting_person")).json()

    assert answer == {"label": f"user:{user.id}", "role": user.role}
    assert acting_person() is None, "the request's person leaked out of the request"


async def test_a_bearer_request_acts_as_the_tokens_owner(app, client, fake_storage_provider):
    _mount_probe(app)
    user = await _user(fake_storage_provider)
    plaintext = mint_plaintext()
    await fake_storage_provider.get_storage(ApiToken).create(ApiToken(
        id="at-owner", user_id=user.id, name="t", token_hash=hash_token(plaintext), prefix=extract_prefix(plaintext), scopes=["mcp"],
        created_at=datetime.now(UTC),
    ))
    client.cookies.clear()

    answer = (await client.get("/v1/_test_acting_person", headers={"Authorization": f"Bearer {plaintext}"})).json()

    assert answer["label"] == f"user:{user.id}", "the token's owner acts, never the token"
    assert acting_person() is None


async def test_an_unauthenticated_request_acts_as_nobody(app, raw_client):
    _mount_probe(app)
    assert (await raw_client.get("/v1/_test_acting_person")).json() == {"label": "nobody", "role": None}


async def test_auth_disabled_acts_as_the_system_person(app, raw_client):
    _mount_probe(app)
    config = app.state.config
    app.state.config = config.model_copy(update={"auth": config.auth.model_copy(update={"enabled": False})})

    assert (await raw_client.get("/v1/_test_acting_person")).json() == {"label": "system", "role": "system"}
    assert acting_person() is None


# The app fixtures live with the API suite.
from tests.api.conftest import app, client, fake_provider_registry, raw_client  # noqa: E402, F401
