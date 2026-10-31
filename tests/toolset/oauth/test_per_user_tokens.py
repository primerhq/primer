"""A browser sign-in belongs to one person (access-control plan P5 Task 2; spec 6.5): token sets are keyed by (toolset, user).

A call or a listing sends ONLY the token of the person it acts for (``person_for_tool``: the run's ``initiated_by`` when the call carries a
``ToolContext``, else the acting person). Nobody known means no browser token: the call answers sign-in required, it never falls back to another
row. Client credentials, and the system person of an auth-disabled install, use the one shared row under the bare toolset id. Refresh lease, fence
and lock are per row, so two people refreshing at once never touch each other's row.

Against the hermetic mock (``tests/_support/mock_oauth_mcp.py``), over the in-memory storage fake.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.access.person import Person
from primer.common.secret_box import SecretBox
from primer.mcp.server import current_actor
from primer.model.mcp_oauth import McpOAuthClient, McpOAuthPendingSignIn, McpOAuthTokenSet, McpSignInRequiredError
from primer.model.principal import Principal, PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.yield_ import ToolContext
from primer.storage import raw_generation
from primer.toolset.mcp import McpToolsetProvider
from primer.toolset.oauth.service import LEASE_TTL, NOT_CONNECTED, ToolsetOAuthService
from primer.toolset.oauth.store import forget_sign_in, forget_user_sign_ins, token_key
from tests._support.mock_oauth_mcp_fixtures import mock_oauth_mcp  # noqa: F401 - the fixture
from tests.toolset.oauth._service_helpers import BINDING, KEY, REDIRECT, Clock, connect, make_toolset, target_of, token_row

A = Person("u-a", "user")
B = Person("u-b", "user")
SYSTEM = Person(None, "system")
CC = {"grant": "client_credentials", "static_client": {"client_id": "cc-client", "client_secret": "cc-secret"}}
PER_USER_MESSAGE = "the MCP server kb needs the person this run acts for to sign in (Toolsets > kb > Connect in the console)"


def _ref(person: Person) -> PrincipalRef:
    if person.role == "system":
        return PrincipalRef.system()
    return PrincipalRef(type="user", id=person.user_id, display=person.user_id, role=person.role, source="local", user_id=person.user_id)


def _ctx(initiated_by: PrincipalRef | None) -> ToolContext:
    return ToolContext(tool_call_id="tc-1", session_id="s-1", workspace_id=None, initiated_by=initiated_by)


async def _all(sp, model) -> list:
    return (await sp.get_storage(model).list(OffsetPage(offset=0, length=100))).items


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def svc(fake_storage_provider, clock):
    service = ToolsetOAuthService(storage_provider=fake_storage_provider, secret_box=SecretBox.from_key_material(KEY), clock=clock, owner_id="pod-a")
    yield service
    await service.aclose()


@pytest.fixture
def kb(mock_oauth_mcp):
    return make_toolset(mock_oauth_mcp.mcp_url, oauth={})


@pytest.fixture
def provider(kb, svc):
    return McpToolsetProvider(kb.id, kb.config, oauth=svc)


async def _access(svc: ToolsetOAuthService, kb, person: Person) -> str:
    row = await svc.store.get_tokens(target_of(kb, person).key, toolset_id=kb.id, user_id=person.user_id)
    assert row is not None
    return svc.store.unseal(row.access_token_enc)


async def test_two_users_hold_separate_tokens(svc, kb):
    await connect(svc, kb, person=A)
    await connect(svc, kb, person=B)

    row_a = await svc.store.get_tokens(token_key("kb", "u-a"), toolset_id="kb", user_id="u-a")
    row_b = await svc.store.get_tokens(token_key("kb", "u-b"), toolset_id="kb", user_id="u-b")
    assert row_a is not None and row_b is not None
    assert (row_a.toolset_id, row_a.user_id, row_b.toolset_id, row_b.user_id) == ("kb", "u-a", "kb", "u-b")
    assert row_a.id != row_b.id and token_key("kb", "u-a") != "kb"
    assert await svc.store.get_tokens("kb") is None, "a browser sign-in of a person is never the shared row"
    assert await svc.access_token(target_of(kb, A)) == await _access(svc, kb, A)
    assert await svc.access_token(target_of(kb, B)) == await _access(svc, kb, B)
    assert await _access(svc, kb, A) != await _access(svc, kb, B)
    assert (await svc.status(kb, A)).state == (await svc.status(kb, B)).state == "connected"


async def test_call_sends_the_runs_persons_token(svc, kb, provider, mock_oauth_mcp):
    await connect(svc, kb, person=A)
    await connect(svc, kb, person=B)
    mock_oauth_mcp.state.seen_tokens.clear()

    result = await provider.call(tool_name="echo", arguments={"text": "hi"}, ctx=_ctx(_ref(B)))

    assert result.output == "hi"
    assert set(mock_oauth_mcp.state.seen_tokens) == {await _access(svc, kb, B)}


async def test_listing_sends_the_acting_persons_token(svc, kb, provider, mock_oauth_mcp):
    await connect(svc, kb, person=A)
    await connect(svc, kb, person=B)
    mock_oauth_mcp.state.seen_tokens.clear()

    reset = current_actor.set(Principal(type="user", id="u-a", display="a", role="user", source="local", user_id="u-a"))
    try:
        tools = sorted([tool.id async for tool in provider.list_tools(principal="b-username-is-not-the-person")])
    finally:
        current_actor.reset(reset)

    assert tools == ["bump", "echo"]
    assert set(mock_oauth_mcp.state.seen_tokens) == {await _access(svc, kb, A)}


async def test_a_runs_listing_sends_the_runs_persons_token(svc, kb, provider, mock_oauth_mcp):
    """The tool manager lists as the run's person (access-control P5 Task 1), whoever the calling task inherited: B's run sends B's token even
    inside A's request."""
    from primer.access.acting import acting_as
    from primer.agent.tool_manager import ToolExecutionManager

    await connect(svc, kb, person=A)
    await connect(svc, kb, person=B)
    mock_oauth_mcp.state.seen_tokens.clear()
    manager = ToolExecutionManager(toolset_providers={"kb": provider}, initiated_by=_ref(B))

    with acting_as(A):
        tools = sorted(tool.id for tool in await manager.list_tools())

    assert tools == ["kb__bump", "kb__echo"] and manager.unavailable_toolsets() == {}
    assert set(mock_oauth_mcp.state.seen_tokens) == {await _access(svc, kb, B)}


async def test_no_person_gets_no_browser_token(svc, kb, provider, mock_oauth_mcp):
    await connect(svc, kb, person=A)
    await connect(svc, kb, person=SYSTEM)
    mock_oauth_mcp.state.seen_tokens.clear()

    with pytest.raises(McpSignInRequiredError) as listed:
        _ = [tool async for tool in provider.list_tools()]
    with pytest.raises(McpSignInRequiredError) as called:
        await provider.call(tool_name="echo", arguments={"text": "hi"}, ctx=_ctx(None))

    assert str(listed.value) == str(called.value) == PER_USER_MESSAGE
    assert called.value.reason == NOT_CONNECTED
    assert mock_oauth_mcp.state.seen_tokens == [], "a call for nobody sent somebody's token"
    with pytest.raises(McpSignInRequiredError):
        await svc.access_token(target_of(kb, Person(None, "user")))


async def test_unattributed_graph_toolcall_gets_no_token(svc, kb, provider, mock_oauth_mcp):
    """Phase 4's fallback for a graph ToolCall node is the unattributed ref: it names nobody, so it gets no token."""
    await connect(svc, kb, person=A)
    await connect(svc, kb, person=SYSTEM)
    mock_oauth_mcp.state.seen_tokens.clear()

    with pytest.raises(McpSignInRequiredError) as caught:
        await provider.call(tool_name="echo", arguments={"text": "hi"}, ctx=_ctx(PrincipalRef.unattributed()))

    assert str(caught.value) == PER_USER_MESSAGE
    assert mock_oauth_mcp.state.seen_tokens == []


async def test_client_credentials_row_is_shared(svc, mock_oauth_mcp):
    cc = make_toolset(mock_oauth_mcp.mcp_url, oauth=CC)
    await svc.connect_client_credentials(target_of(cc, A), connected_by="u-a")
    provider = McpToolsetProvider(cc.id, cc.config, oauth=svc)

    for person in (A, B, Person(None, "user")):
        assert (await provider.call(tool_name="echo", arguments={"text": "x"}, ctx=_ctx(_ref(person) if person.user_id else None))).output == "x"

    row = await svc.store.get_tokens("kb")
    assert row is not None and (row.id, row.toolset_id, row.user_id) == ("kb", "kb", None)
    assert len(set(mock_oauth_mcp.state.seen_tokens)) == 1
    assert target_of(cc, A).key == target_of(cc, B).key == "kb"
    assert (await svc.status(cc, B)).state == "connected"


async def test_system_person_uses_the_shared_row(svc, kb, provider, mock_oauth_mcp):
    await connect(svc, kb, person=SYSTEM)
    row = await svc.store.get_tokens("kb")
    assert row is not None and (row.toolset_id, row.user_id) == ("kb", None)
    mock_oauth_mcp.state.seen_tokens.clear()

    assert (await provider.call(tool_name="echo", arguments={"text": "x"}, ctx=_ctx(PrincipalRef.system()))).output == "x"
    assert set(mock_oauth_mcp.state.seen_tokens) == {svc.store.unseal(row.access_token_enc)}
    sent = len(mock_oauth_mcp.state.seen_tokens)

    with pytest.raises(McpSignInRequiredError):
        await provider.call(tool_name="echo", arguments={"text": "x"}, ctx=_ctx(_ref(A)))
    assert len(mock_oauth_mcp.state.seen_tokens) == sent, "a person never falls back to the shared row"


async def test_refresh_lease_and_lock_are_per_user(svc, kb, clock, mock_oauth_mcp):
    """A's lease is held by another process and A's lock by another caller of this one: B still refreshes at once, and A's row is untouched."""
    await connect(svc, kb, person=A)
    await connect(svc, kb, person=B)
    a, b = target_of(kb, A), target_of(kb, B)
    assert svc._lock_for(a.key) is not svc._lock_for(b.key)
    clock.advance(minutes=59, seconds=30)                       # both tokens now expire within REFRESH_SKEW
    row_a = await svc.store.get_tokens(a.key, toolset_id="kb", user_id="u-a")
    held = await svc.store.patch_tokens(
        a.key, {"refresh_lease_until": clock.now + LEASE_TTL, "refresh_lease_owner": "pod-b#1"}, where={"version": [raw_generation(row_a, "version")]},
    )
    assert held is not None
    old_b = await _access(svc, kb, B)

    async with svc._lock_for(a.key):
        async with asyncio.timeout(10):
            fresh_b = await svc.access_token(b)

    assert fresh_b != old_b and fresh_b == await _access(svc, kb, B)
    after_a = await svc.store.get_tokens(a.key, toolset_id="kb", user_id="u-a")
    assert (after_a.version, after_a.refresh_lease_owner, after_a.access_token_enc) == (held.version, "pod-b#1", held.access_token_enc)


async def test_disconnect_ends_only_that_persons_sign_in(svc, kb, fake_storage_provider):
    await connect(svc, kb, person=A)
    await connect(svc, kb, person=B)
    started = await svc.start_sign_in(target_of(kb, A), redirect_uri=REDIRECT, started_by="u-a", browser_binding=BINDING)
    assert started.authorize_url

    status = await svc.disconnect(kb, B)

    assert status.state == "needs_sign_in"
    assert await svc.store.get_tokens(token_key("kb", "u-b"), toolset_id="kb", user_id="u-b") is None
    assert await svc.store.get_tokens(token_key("kb", "u-a"), toolset_id="kb", user_id="u-a") is not None
    assert [row.user_id for row in await _all(fake_storage_provider, McpOAuthPendingSignIn)] == ["u-a"], "B's disconnect left A's pending sign-in alone"


async def _everything(sp) -> dict[str, list[str]]:
    return {model.__name__: sorted(r.id for r in await _all(sp, model))
            for model in (McpOAuthTokenSet, McpOAuthClient, McpOAuthPendingSignIn)}


@pytest.mark.parametrize("how", ["service", "keyless"])
async def test_forget_removes_every_users_sign_in(how, svc, kb, fake_storage_provider):
    await connect(svc, kb, person=A)
    await connect(svc, kb, person=B)
    await connect(svc, kb, person=SYSTEM)
    await svc.start_sign_in(target_of(kb, B), redirect_uri=REDIRECT, started_by="u-b", browser_binding=BINDING)
    other = kb.model_copy(update={"id": "kb2"})
    await svc.store.replace_tokens(token_row(svc.store, target_of(other, A)))
    before = await _everything(fake_storage_provider)
    assert len(before["McpOAuthTokenSet"]) == 4 and before["McpOAuthClient"] == ["kb"] and len(before["McpOAuthPendingSignIn"]) == 1

    if how == "service":
        await svc.forget("kb")
    else:
        await forget_sign_in(fake_storage_provider, "kb")

    assert await _everything(fake_storage_provider) == {
        "McpOAuthTokenSet": [token_key("kb2", "u-a")], "McpOAuthClient": [], "McpOAuthPendingSignIn": [],
    }


async def test_forget_user_sign_ins_removes_only_theirs(svc, kb, fake_storage_provider):
    await connect(svc, kb, person=A)
    await connect(svc, kb, person=B)
    other = kb.model_copy(update={"id": "kb2"})
    await svc.store.replace_tokens(token_row(svc.store, target_of(other, A)))
    await svc.start_sign_in(target_of(kb, A), redirect_uri=REDIRECT, started_by="u-a", browser_binding=BINDING)
    await svc.start_sign_in(target_of(kb, B), redirect_uri=REDIRECT, started_by="u-b", browser_binding=BINDING)

    await forget_user_sign_ins(fake_storage_provider, "u-a")

    assert [(r.toolset_id, r.user_id) for r in await _all(fake_storage_provider, McpOAuthTokenSet)] == [("kb", "u-b")]
    assert [r.user_id for r in await _all(fake_storage_provider, McpOAuthPendingSignIn)] == ["u-b"]
    assert (await _everything(fake_storage_provider))["McpOAuthClient"] == ["kb"], "the registered client is primer's, not a person's"


async def test_row_naming_another_pair_is_ignored(svc, kb, mock_oauth_mcp):
    """A row is served only when its fields name the pair it was looked up for: B's tokens stored under A's key, or a person's tokens under the bare
    id, are never sent."""
    a = target_of(kb, A)
    await svc.store.replace_tokens(token_row(svc.store, a, toolset_id="kb", user_id="u-b"))
    await svc.store.replace_tokens(token_row(svc.store, target_of(kb, SYSTEM), user_id="u-a"))
    await svc.store.replace_tokens(token_row(svc.store, target_of(kb, B), toolset_id="kb2"))

    assert await svc.store.get_tokens(a.key, toolset_id="kb", user_id="u-a") is None
    assert await svc.store.get_tokens("kb") is None
    assert await svc.store.get_tokens(token_key("kb", "u-b"), toolset_id="kb", user_id="u-b") is None
    for person in (A, B, SYSTEM):
        with pytest.raises(McpSignInRequiredError):
            await svc.access_token(target_of(kb, person))
        assert (await svc.status(kb, person)).state == "needs_sign_in"


async def test_a_browser_sign_in_needs_a_person(svc, kb):
    from primer.model.except_ import BadRequestError

    with pytest.raises(BadRequestError):
        await svc.start_sign_in(target_of(kb, Person(None, "user")), redirect_uri=REDIRECT, started_by=None, browser_binding=BINDING)


async def test_the_callback_writes_the_row_of_the_person_who_started_it(svc, kb, clock):
    """``complete_sign_in`` carries no person: the pending sign-in names whose row it writes."""
    await connect(svc, kb, person=B)
    row = await svc.store.get_tokens(token_key("kb", "u-b"), toolset_id="kb", user_id="u-b")
    assert row is not None and (row.connected_by, row.connected_at) == ("u-b", clock.now)


ELSEWHERE = "http://evil.example/v1/toolsets/kb/oauth/callback"


async def test_a_start_that_may_not_reregister_signs_nobody_out(svc, kb, clock, mock_oauth_mcp, fake_storage_provider):
    """Review finding 1: B, below FULL, starts from another redirect URI (a forged Host). Re-registering would replace the one client A's tokens
    name, so A's next refresh would fail and wipe A's sign-in, and A's own sign-in in flight would fail its exchange. B is refused with a 409
    naming the registered origin; the registration, A's tokens and A's pending sign-in all stand."""
    from primer.model.except_ import ConflictError
    from primer.model.user import User
    from tests.toolset.oauth._service_helpers import NOW, authorize

    mock_oauth_mcp.state.register_confidential = True
    mock_oauth_mcp.state.access_token_ttl_s = 60
    await connect(svc, kb, person=A)
    registered = await svc.store.get_client("kb")
    await fake_storage_provider.get_storage(User).create(User(id="u-b", username="b", created_at=NOW, role="user"))
    a_again = await svc.start_sign_in(target_of(kb, A), redirect_uri=REDIRECT, started_by="u-a", browser_binding=BINDING)

    with pytest.raises(ConflictError, match="sign in through http://127.0.0.1:9"):
        await svc.start_sign_in(target_of(kb, B), redirect_uri=ELSEWHERE, started_by="u-b", browser_binding="b-binding-0123456789", may_reregister=False)

    assert (await svc.store.get_client("kb")).client_id == registered.client_id and len(mock_oauth_mcp.state.registrations) == 1
    back = authorize(a_again.authorize_url)
    done = await svc.complete_sign_in(kb, toolset_id="kb", state=back["state"], code=back["code"], error=None, browser_binding=BINDING)
    assert done.state == "connected", "A's sign-in in flight still exchanges its code"
    clock.advance(hours=2)
    assert await svc.access_token(target_of(kb, A)), "A's refresh still authenticates with the registered client"


async def test_full_access_or_a_public_url_may_reregister(svc, kb, fake_storage_provider, mock_oauth_mcp):
    """Someone with FULL re-registers as before; with PRIMER_PUBLIC_URL set the redirect URI is the deployment's, not the caller's, so a start
    behaves as it always did."""
    from tests.toolset.oauth._service_helpers import KEY

    await svc.start_sign_in(target_of(kb, A), redirect_uri=REDIRECT, started_by="u-a", browser_binding=BINDING)
    await svc.start_sign_in(target_of(kb, A), redirect_uri=ELSEWHERE, started_by="u-a", browser_binding=BINDING, may_reregister=True)
    assert (await svc.store.get_client("kb")).redirect_uri == ELSEWHERE

    public = ToolsetOAuthService(storage_provider=fake_storage_provider, secret_box=SecretBox.from_key_material(KEY), public_url="https://primer.example")
    try:
        moved = public.redirect_uri("kb", request_base_url=None)
        await public.start_sign_in(target_of(kb, B), redirect_uri=moved, started_by="u-b", browser_binding=BINDING, may_reregister=False)
    finally:
        await public.aclose()
    assert (await svc.store.get_client("kb")).redirect_uri == moved and len(mock_oauth_mcp.state.registrations) == 3


async def test_an_unreadable_registration_is_renewed_only_by_full_access(svc, kb, fake_storage_provider, mock_oauth_mcp):
    from primer.model.except_ import ConflictError
    from tests.toolset.oauth._service_helpers import NOW, OTHER_KEY
    from primer.toolset.oauth.store import McpOAuthStore

    other = McpOAuthStore(fake_storage_provider, SecretBox.from_key_material(OTHER_KEY))
    await other.put_client(McpOAuthClient(id="kb", issuer=mock_oauth_mcp.issuer, redirect_uri=REDIRECT, client_id="old-client",
                                          client_secret_enc=other.seal("old-secret"), registered_at=NOW))

    with pytest.raises(ConflictError, match="full access"):
        await svc.start_sign_in(target_of(kb, B), redirect_uri=REDIRECT, started_by="u-b", browser_binding=BINDING, may_reregister=False)
    assert mock_oauth_mcp.state.registrations == []
