"""The sign-in routes act for the person (access-control plan P5 Task 3; Decisions 3 and 5).

A browser sign-in is the caller's own: any full user who may USE the toolset (a level on it, or a public tool of it) starts, sees and ends their
own sign-in. A client-credentials sign-in is shared: connecting or disconnecting it needs FULL on the toolset, its status needs use. A toolset the
caller may not use answers exactly like a missing one on every route. The public callback writes the row of the person who started the sign-in,
and only when that person still exists, is enabled and full, may still use the toolset, and is not someone else signed in on the callback's browser.
"""

from __future__ import annotations

import pytest

from primer.access.person import Person
from primer.model.access import Access
from primer.model.mcp_oauth import McpOAuthPendingSignIn
from primer.model.provider import Toolset
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.toolset.oauth.store import token_key
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401
from tests.api.test_mcp_oauth_routes import BINDING_COOKIE, oauth_app  # noqa: F401
from tests.api.test_require_user_admin import _login, _seed
from tests.toolset.oauth._service_helpers import authorize, connect, make_toolset

CC = {"grant": "client_credentials", "static_client": {"client_id": "cc-client", "client_secret": "cc-secret"}}
MISSING = "Toolset 'kb' does not exist"


async def _as(raw_client, app, name: str, role: str = "user") -> None:
    if await app.state.storage_provider.get_storage(User).get(f"u-{name}") is None:
        await _seed(app, uid=f"u-{name}", username=name, role=role)
    await _login(raw_client, name)


@pytest.fixture
def store_toolset(oauth_app, mock_oauth_mcp):  # noqa: F811
    """Store the toolset ``kb`` on the mock server, owned by ``u-owner`` unless told otherwise."""
    async def store(*, oauth=None, access: Access | None = None) -> Toolset:
        toolset = make_toolset(mock_oauth_mcp.mcp_url, oauth={} if oauth is None else oauth)
        toolset.access = access or Access(owners=["u-owner"])
        await oauth_app.state.storage_provider.get_storage(Toolset).create(toolset)
        return toolset

    return store


async def _row(app, user_id: str | None):
    store = app.state.mcp_oauth.store
    if user_id is None:
        return await store.get_tokens("kb")
    return await store.get_tokens(token_key("kb", user_id), toolset_id="kb", user_id=user_id)


async def _start_and_authorize(raw_client) -> dict[str, str]:
    started = await raw_client.post("/v1/toolsets/kb/oauth/start")
    assert started.status_code == 200, started.text
    return authorize(started.json()["authorize_url"])


async def _callback(raw_client, back: dict[str, str]):
    return await raw_client.get("/v1/toolsets/kb/oauth/callback", params={"state": back["state"], "code": back["code"]})


def _binding(raw_client):
    [cookie] = [c for c in raw_client.cookies.jar if c.name == BINDING_COOKIE]
    return cookie


async def test_grantee_may_start_a_browser_sign_in(raw_client, oauth_app, store_toolset):
    await store_toolset(access=Access(owners=["u-owner"], grants={"u-bob": "read"}))
    await _as(raw_client, oauth_app, "bob")

    back = await _start_and_authorize(raw_client)
    done = await _callback(raw_client, back)

    assert done.status_code == 200, done.text
    row = await _row(oauth_app, "u-bob")
    assert row is not None and (row.connected_by, row.user_id) == ("u-bob", "u-bob")
    status = (await raw_client.get("/v1/toolsets/kb/oauth/status")).json()
    assert (status["state"], status["per_user"]) == ("connected", True)
    assert await _row(oauth_app, "u-owner") is None and await _row(oauth_app, None) is None


async def test_public_tool_user_may_sign_in(raw_client, oauth_app, store_toolset):
    """Using the public tool needs the user's own token, so a user whose only access is that tool may sign in (Lead answer 5)."""
    await store_toolset(access=Access(owners=["u-owner"], public_tools=["echo"]))
    await _as(raw_client, oauth_app, "carol")

    done = await _callback(raw_client, await _start_and_authorize(raw_client))

    assert done.status_code == 200, done.text
    assert (await raw_client.get("/v1/toolsets/kb/oauth/status")).json()["state"] == "connected"
    assert (await raw_client.post("/v1/toolsets/kb/oauth/disconnect")).json()["state"] == "needs_sign_in"
    assert await _row(oauth_app, "u-carol") is None


@pytest.mark.parametrize("grant", ["browser", "client_credentials"])
async def test_stranger_gets_404_on_every_route(raw_client, oauth_app, store_toolset, grant):
    toolset = await store_toolset(oauth=CC if grant == "client_credentials" else None)
    if grant == "browser":
        await connect(oauth_app.state.mcp_oauth, toolset, person=Person("u-owner", "user"))
    await _as(raw_client, oauth_app, "stranger")

    answers = [
        await raw_client.post("/v1/toolsets/kb/oauth/start"),
        await raw_client.get("/v1/toolsets/kb/oauth/status"),
        await raw_client.post("/v1/toolsets/kb/oauth/disconnect"),
    ]

    assert [a.status_code for a in answers] == [404, 404, 404], [a.text for a in answers]
    assert {a.json()["detail"] for a in answers} == {MISSING}, "exactly like a missing toolset"
    assert BINDING_COOKIE not in {c.name for c in raw_client.cookies.jar}
    assert (await oauth_app.state.storage_provider.get_storage(McpOAuthPendingSignIn).list(OffsetPage(offset=0, length=10))).items == []


async def test_restricted_user_refused(raw_client, oauth_app, store_toolset):
    await store_toolset(access=Access(owners=["u-owner"], public=True))
    await _as(raw_client, oauth_app, "rita", role="restricted")

    for method, path in (("post", "start"), ("get", "status"), ("post", "disconnect")):
        answer = await getattr(raw_client, method)(f"/v1/toolsets/kb/oauth/{path}")
        assert answer.status_code == 403, answer.text
        assert answer.json()["extensions"]["error"] == "forbidden_role"


async def test_client_credentials_connect_needs_full(raw_client, oauth_app, store_toolset):
    await store_toolset(oauth=CC, access=Access(owners=["u-owner"], grants={"u-bob": "read", "u-dave": "full"}))

    await _as(raw_client, oauth_app, "bob")
    refused = await raw_client.post("/v1/toolsets/kb/oauth/start")
    assert refused.status_code == 403, refused.text
    assert await _row(oauth_app, None) is None

    await _as(raw_client, oauth_app, "dave")
    connected = await raw_client.post("/v1/toolsets/kb/oauth/start")
    assert connected.status_code == 200, connected.text
    assert connected.json()["status"]["state"] == "connected" and connected.json()["status"]["per_user"] is False
    assert (await _row(oauth_app, None)).connected_by == "u-dave"

    await _as(raw_client, oauth_app, "bob")
    status = (await raw_client.get("/v1/toolsets/kb/oauth/status")).json()
    assert (status["state"], status["per_user"]) == ("connected", False), "the shared sign-in, read by anyone who may use it"
    assert (await raw_client.post("/v1/toolsets/kb/oauth/disconnect")).status_code == 403
    assert await _row(oauth_app, None) is not None

    await _as(raw_client, oauth_app, "dave")
    assert (await raw_client.post("/v1/toolsets/kb/oauth/disconnect")).json()["state"] == "needs_sign_in"
    assert await _row(oauth_app, None) is None


async def test_disconnect_ends_only_the_callers_sign_in(raw_client, oauth_app, store_toolset):
    toolset = await store_toolset(access=Access(owners=["u-owner"], grants={"u-bob": "read"}))
    service = oauth_app.state.mcp_oauth
    await _as(raw_client, oauth_app, "bob")
    await _as(raw_client, oauth_app, "owner")
    await connect(service, toolset, person=Person("u-owner", "user"))
    await connect(service, toolset, person=Person("u-bob", "user"))
    await raw_client.post("/v1/toolsets/kb/oauth/start")          # the owner's Reconnect, left open on the provider's page
    await _as(raw_client, oauth_app, "bob")
    await raw_client.post("/v1/toolsets/kb/oauth/start")          # bob's

    answer = await raw_client.post("/v1/toolsets/kb/oauth/disconnect")

    assert answer.status_code == 200 and answer.json()["state"] == "needs_sign_in", answer.text
    assert await _row(oauth_app, "u-bob") is None
    assert await _row(oauth_app, "u-owner") is not None
    pending = (await oauth_app.state.storage_provider.get_storage(McpOAuthPendingSignIn).list(OffsetPage(offset=0, length=10))).items
    assert [row.user_id for row in pending] == ["u-owner"], "the owner's pending sign-in is left alone"


async def _revoke_bob(app):
    storage = app.state.storage_provider.get_storage(Toolset)
    row = await storage.get("kb")
    await storage.update(row.model_copy(update={"access": Access(owners=["u-owner"])}))


async def _disable_bob(app):
    users = app.state.storage_provider.get_storage(User)
    await users.update((await users.get("u-bob")).model_copy(update={"disabled": True}))


async def _restrict_bob(app):
    users = app.state.storage_provider.get_storage(User)
    await users.update((await users.get("u-bob")).model_copy(update={"role": "restricted"}))


async def _delete_bob(app):
    await app.state.storage_provider.get_storage(User).delete("u-bob")


@pytest.mark.parametrize("lose", [_revoke_bob, _disable_bob, _restrict_bob, _delete_bob], ids=["revoked", "disabled", "restricted", "deleted"])
async def test_callback_after_access_lost_writes_nothing(raw_client, oauth_app, store_toolset, lose):
    await store_toolset(access=Access(owners=["u-owner"], grants={"u-bob": "read"}))
    await _as(raw_client, oauth_app, "bob")
    back = await _start_and_authorize(raw_client)
    binding = _binding(raw_client)
    raw_client.cookies.clear()                                    # the provider's redirect: the binding cookie, no primer session needed
    raw_client.cookies.jar.set_cookie(binding)

    await lose(oauth_app)
    answer = await _callback(raw_client, back)

    assert answer.status_code == 400, answer.text
    assert answer.json()["extensions"]["code"] == "invalid_state"
    assert await _row(oauth_app, "u-bob") is None


async def test_callback_by_another_signed_in_person_writes_nothing(raw_client, oauth_app, store_toolset):
    """Bob started the sign-in; the callback arrives in that browser while Carol is the one signed in to primer there: nothing is connected for
    either of them."""
    await store_toolset(access=Access(owners=["u-owner"], grants={"u-bob": "read", "u-carol": "read"}))
    await _as(raw_client, oauth_app, "bob")
    back = await _start_and_authorize(raw_client)
    binding = _binding(raw_client)
    await _as(raw_client, oauth_app, "carol")
    raw_client.cookies.jar.set_cookie(binding)

    answer = await _callback(raw_client, back)

    assert answer.status_code == 400, answer.text
    assert answer.json()["extensions"]["code"] == "invalid_state"
    assert await _row(oauth_app, "u-bob") is None and await _row(oauth_app, "u-carol") is None


async def test_status_and_catalogue_auth_are_the_callers(raw_client, oauth_app, store_toolset):
    toolset = await store_toolset(access=Access(owners=["u-owner"], grants={"u-bob": "read"}))
    await _seed(oauth_app, uid="u-owner", username="owner", role="user")
    await connect(oauth_app.state.mcp_oauth, toolset, person=Person("u-owner", "user"))

    for name, state in (("owner", "connected"), ("bob", "needs_sign_in")):
        await _as(raw_client, oauth_app, name)
        status = (await raw_client.get("/v1/toolsets/kb/oauth/status")).json()
        assert (status["state"], status["per_user"]) == (state, True), name
        entry = next(item for item in (await raw_client.get("/v1/tools")).json()["items"] if item["id"] == "kb")
        assert entry["auth"]["state"] == state, name


async def _client_id(app) -> str:
    return (await app.state.mcp_oauth.store.get_client("kb")).client_id


async def test_a_user_below_full_never_replaces_the_registration(raw_client, oauth_app, store_toolset, mock_oauth_mcp):
    """Review finding 1: without PRIMER_PUBLIC_URL the redirect URI comes from the request's Host. A start from another Host by someone below FULL
    would register the toolset's client again, replacing the one every other person's tokens name, so their next refresh fails and signs them out:
    it is refused with a 409 that names the registered origin, and the registration stays."""
    mock_oauth_mcp.state.register_confidential = True
    await store_toolset(access=Access(owners=["u-owner"], public_tools=["echo"]))
    await _as(raw_client, oauth_app, "carol")
    assert (await raw_client.post("/v1/toolsets/kb/oauth/start")).status_code == 200
    registered = await _client_id(oauth_app)

    moved = await raw_client.post("/v1/toolsets/kb/oauth/start", headers={"Host": "evil.example"})

    assert moved.status_code == 409, moved.text
    assert "sign in through http://test" in moved.json()["detail"]
    assert await _client_id(oauth_app) == registered and len(mock_oauth_mcp.state.registrations) == 1


async def test_full_access_may_move_the_registration(raw_client, oauth_app, store_toolset, mock_oauth_mcp):
    await store_toolset(access=Access(owners=["u-owner"], public_tools=["echo"]))
    await _as(raw_client, oauth_app, "carol")
    assert (await raw_client.post("/v1/toolsets/kb/oauth/start")).status_code == 200
    registered = await _client_id(oauth_app)
    await _as(raw_client, oauth_app, "owner")

    moved = await raw_client.post("/v1/toolsets/kb/oauth/start", headers={"Host": "primer.example"})

    assert moved.status_code == 200, moved.text
    assert moved.json()["redirect_uri"] == "http://primer.example/v1/toolsets/kb/oauth/callback"
    assert await _client_id(oauth_app) != registered
