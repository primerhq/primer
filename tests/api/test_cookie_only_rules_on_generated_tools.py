"""Review 2026-11-23 finding 4.3: the cookie-only rules must hold on the
generated route tool path too.

A trusted route-tool request carries no token of its own (the middleware
rebuilds the caller from the tool's persisted initiator), so a call
initiated by a bearer token used to pass ``require_cookie_session`` and
mint a plaintext token straight into model context or the MCP client. The
dispatch now carries the initiating credential's kind and the guard
refuses an ``api_token`` origin, while a cookie (user) origin still works.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

# Convention: shared API test fixtures.
from tests.api.conftest import app, client, raw_client  # noqa: F401

from primer.model.api_token import ApiToken
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.user import User
from primer.model.workspace import (
    Workspace as WorkspaceRowModel,
    WorkspaceRuntimeMeta,
)
from primer.model.yield_ import ToolContext
from primer.storage.q import Q


def _ctx(initiated_by: PrincipalRef) -> ToolContext:
    return ToolContext(
        tool_call_id="call-review-4-3",
        session_id=None,
        workspace_id=None,
        initiated_by=initiated_by,
    )


def _bearer_ref() -> PrincipalRef:
    """A run initiated by an API token: the owner's identity, api_token kind."""
    return PrincipalRef(
        type="api_token",
        id="tok-bearer",
        display="tok-bearer",
        role="user",
        source="local",
        user_id="user-admin",
    )


def _cookie_ref() -> PrincipalRef:
    return PrincipalRef(
        type="user",
        id="user-admin",
        display="adminactor",
        role="user",
        source="local",
        user_id="user-admin",
    )


async def _seed_user(app) -> None:
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-admin",
            username="adminactor",
            created_at=datetime.now(timezone.utc),
            role="user",
        )
    )


@pytest.mark.asyncio
async def test_bearer_originated_create_token_tool_is_refused(app):
    """A bearer-initiated call of the generated ``auth__create_api_token``
    tool must be refused (the cookie-only rule) and mint no token."""
    await _seed_user(app)
    provider = app.state.generated_route_toolsets.providers["auth"]
    try:
        result = await provider.call(
            tool_name="create_api_token",
            arguments={"body": {"name": "leaky"}},
            ctx=_ctx(_bearer_ref()),
        )
    except BaseException as exc:  # a refusal may raise; either way nothing may mint
        result = None
        raised = exc
    else:
        raised = None

    assert raised is not None or (result is not None and result.is_error), (
        f"the bearer-originated tool call was not refused (raised: {raised!r}, "
        f"result: {result!r})"
    )
    if result is not None:
        assert "token_minting_forbidden" in result.output, result.output
    tokens = app.state.storage_provider.get_storage(ApiToken)
    page = await tokens.find(
        Q(ApiToken).where("user_id", "user-admin").build(),
        OffsetPage(offset=0, length=200),
    )
    assert not any(row.user_id == "user-admin" for row in page.items), (
        "a bearer-originated tool call minted an api token"
    )


@pytest.mark.asyncio
async def test_cookie_originated_create_token_tool_still_works(app):
    """A user (cookie) initiated call of the same tool still mints: the
    tightening must not refuse the origin it exists for."""
    await _seed_user(app)
    provider = app.state.generated_route_toolsets.providers["auth"]
    result = await provider.call(
        tool_name="create_api_token",
        arguments={"body": {"name": "cookie-minted"}},
        ctx=_ctx(_cookie_ref()),
    )

    assert result is not None and not result.is_error, (
        f"the cookie-originated tool call was refused (result: {result!r})"
    )
    tokens = app.state.storage_provider.get_storage(ApiToken)
    page = await tokens.find(
        Q(ApiToken).where("user_id", "user-admin").build(),
        OffsetPage(offset=0, length=200),
    )
    assert any(row.name == "cookie-minted" for row in page.items), (
        "the cookie-originated tool call did not mint the token"
    )


@pytest.mark.asyncio
async def test_cookie_rest_path_still_works(client):
    """The REST cookie path the rule was built around is untouched."""
    r = await client.post(
        "/v1/auth/tokens", json={"name": "rest-cookie"},
    )
    assert r.status_code == 201, r.text
    assert r.json().get("name") == "rest-cookie"


def _admin_bearer_ref() -> PrincipalRef:
    """An admin bearer initiator: it must clear the route's require_admin
    so the refusal comes from the cookie-only guard itself, not the tier."""
    return PrincipalRef(
        type="api_token",
        id="tok-admin-bearer",
        display="tok-admin-bearer",
        role="admin",
        source="local",
        user_id="user-admin-exp",
    )


async def _seed_admin_user(app) -> None:
    """The trusted path re-reads the initiator's user row and refuses it
    when the row's role differs from the persisted one, so the admin ref
    needs an admin user."""
    await app.state.storage_provider.get_storage(User).create(
        User(
            id="user-admin-exp",
            username="adminexpactor",
            created_at=datetime.now(timezone.utc),
            role="admin",
        )
    )


@pytest.mark.asyncio
async def test_bearer_originated_update_exposure_tool_is_refused(app, client):
    """Review round 1 (4.3 on the exposure router): a bearer-initiated call
    of the generated ``mcp__update_exposure`` tool must be refused by the
    cookie-only guard (the same shared origin check as the token router)
    and the exposure row must stay untouched."""
    await _seed_admin_user(app)
    baseline = await client.get("/v1/mcp_exposure")
    assert baseline.status_code == 200, baseline.text
    assert baseline.json()["enabled"] is False

    provider = app.state.generated_route_toolsets.providers["mcp"]
    try:
        result = await provider.call(
            tool_name="update_exposure",
            arguments={"body": {"enabled": True}},
            ctx=_ctx(_admin_bearer_ref()),
        )
    except BaseException as exc:  # a refusal may raise; either way nothing may change
        result = None
        raised = exc
    else:
        raised = None

    assert raised is not None or (result is not None and result.is_error), (
        f"the bearer-originated tool call was not refused (raised: {raised!r}, "
        f"result: {result!r})"
    )
    if result is not None:
        assert "mcp_exposure_cookie_only" in result.output, result.output
    after = await client.get("/v1/mcp_exposure")
    assert after.json()["enabled"] is False, (
        "a bearer-originated tool call edited the MCP exposure config"
    )


@pytest.mark.asyncio
async def test_cookie_rest_path_can_still_update_exposure(client):
    """The cookie REST path the guard exists for still edits the config:
    the tightening must not refuse the origin it is built around."""
    r = await client.put("/v1/mcp_exposure", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    got = await client.get("/v1/mcp_exposure")
    assert got.json()["enabled"] is True


# --- Review round 1 part 2 (4.3 bypassable in one hop) --------------------
#
# Before the fix the trusted route-tool branch always rebuilt
# state.actor as type "user", so a session created by a bearer-originated
# tool call was PERSISTED with a user initiator; a turn in that session then
# saw _originating_credential_kind == "user" and the cookie-only rules
# passed, minting a token whose plaintext lands in a transcript the bearer
# can read. The dispatch now carries the whole originating PrincipalRef and
# the middleware rebuilds the actor with the initiator's real kind, so a
# call that originated from an api_token is an api_token actor everywhere.


from primer.bootstrap.defaults import RESERVED_OPERATOR_AGENT  # noqa: E402
from primer.model.agent import Agent, AgentModel  # noqa: E402
from primer.model.workspace_session import WorkspaceSession  # noqa: E402
from tests.session._peer_fakes import FakeRegistry  # noqa: E402

WS = "ws-43"


def _bearer_owner_ref() -> PrincipalRef:
    return PrincipalRef(
        type="api_token",
        id="tok-hop",
        display="tok-hop",
        role="admin",
        source="local",
        user_id="user-hop",
    )


def _cookie_owner_ref() -> PrincipalRef:
    return PrincipalRef(
        type="user",
        id="user-hop",
        display="hopowner",
        role="admin",
        source="local",
        user_id="user-hop",
    )


async def _seed_world(app) -> None:
    """An admin owner, a workspace row and the operator agent, with the
    registry's backend side faked the way the session access tests do it."""
    sp = app.state.storage_provider
    await sp.get_storage(User).create(
        User(
            id="user-hop",
            username="hopowner",
            created_at=datetime.now(timezone.utc),
            role="admin",
        )
    )
    await sp.get_storage(WorkspaceRowModel).create(
        WorkspaceRowModel(
            id=WS,
            template_id="tpl-1",
            provider_id="wp-hop",
            created_at=datetime.now(timezone.utc),
            phase="running",
            access=Access(owners=["user-hop"]),
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959/", token="t"),
        )
    )
    if await sp.get_storage(Agent).get(RESERVED_OPERATOR_AGENT) is None:
        await sp.get_storage(Agent).create(
            Agent(
                id=RESERVED_OPERATOR_AGENT,
                description=RESERVED_OPERATOR_AGENT,
                model=AgentModel(profile_id="p--m"),
                tools=[],
                system_prompt=[],
            )
        )
    registry = FakeRegistry()
    app.state.workspace_registry.get_workspace = registry.get_workspace  # type: ignore[method-assign]


async def _create_session_via_tool(app, ref: PrincipalRef) -> WorkspaceSession:
    """Hop 1: the tool-mediated session create, driven exactly the way a
    generated tool call drives it (trusted in-process dispatch)."""
    provider = app.state.generated_route_toolsets.providers["workspace_sessions"]
    result = await provider.call(
        tool_name="ensure_main_session",
        arguments={"workspace_id": WS},
        ctx=_ctx(ref),
    )
    assert result is not None and not result.is_error, (
        f"the tool-mediated session create failed (result: {result!r})"
    )
    body = json.loads(result.output)
    return await app.state.storage_provider.get_storage(WorkspaceSession).get(body["id"])


@pytest.mark.asyncio
async def test_bearer_origin_session_chain_cannot_mint_tokens(app):
    """The two-hop chain (review round 1, part 2): a bearer-originated
    tool call creates a session whose PERSISTED initiator keeps the
    api_token kind, and a turn in that session is then refused by the
    cookie-only rules when it tries to mint a token."""
    await _seed_world(app)
    session = await _create_session_via_tool(app, _bearer_owner_ref())

    # Hop 1 persisted the initiator with its real kind: the exploit reads
    # exactly this row, so this assertion is the fix's load-bearing wall.
    assert session.initiated_by is not None and session.initiated_by.type == "api_token", (
        f"the bearer-originated session was persisted with the wrong initiator kind "
        f"(initiated_by: {session.initiated_by!r})"
    )
    assert session.initiated_by.id == "tok-hop"

    # Hop 2: a turn in that session carries the persisted initiator. The
    # mint attempt must be refused and must mint nothing.
    turn_ctx = ToolContext(
        tool_call_id="call-hop-2",
        session_id=session.id,
        workspace_id=WS,
        initiated_by=session.initiated_by,
    )
    provider = app.state.generated_route_toolsets.providers["auth"]
    try:
        result = await provider.call(
            tool_name="create_api_token",
            arguments={"body": {"name": "hop2-leak"}},
            ctx=turn_ctx,
        )
    except BaseException as exc:  # a refusal may raise; either way nothing may mint
        result = None
        raised = exc
    else:
        raised = None

    assert raised is not None or (result is not None and result.is_error), (
        f"the turn in the bearer-originated session was not refused "
        f"(raised: {raised!r}, result: {result!r})"
    )
    if result is not None:
        assert "token_minting_forbidden" in result.output, result.output
    tokens = app.state.storage_provider.get_storage(ApiToken)
    page = await tokens.find(
        Q(ApiToken).where("user_id", "user-hop").build(),
        OffsetPage(offset=0, length=200),
    )
    assert not any(row.name == "hop2-leak" for row in page.items), (
        "a turn in the bearer-originated session minted an api token"
    )


@pytest.mark.asyncio
async def test_cookie_origin_session_chain_still_mints(app):
    """The tightening's benign side: a cookie (user) initiator's session
    keeps the user kind, and a turn in it can still mint through the same
    tool."""
    await _seed_world(app)
    session = await _create_session_via_tool(app, _cookie_owner_ref())
    assert session.initiated_by is not None and session.initiated_by.type == "user", (
        f"the cookie-originated session lost its initiator kind "
        f"(initiated_by: {session.initiated_by!r})"
    )

    turn_ctx = ToolContext(
        tool_call_id="call-hop-2b",
        session_id=session.id,
        workspace_id=WS,
        initiated_by=session.initiated_by,
    )
    provider = app.state.generated_route_toolsets.providers["auth"]
    result = await provider.call(
        tool_name="create_api_token",
        arguments={"body": {"name": "hop2-cookie"}},
        ctx=turn_ctx,
    )
    assert result is not None and not result.is_error, (
        f"the cookie-originated turn was refused (result: {result!r})"
    )
    tokens = app.state.storage_provider.get_storage(ApiToken)
    page = await tokens.find(
        Q(ApiToken).where("user_id", "user-hop").build(),
        OffsetPage(offset=0, length=200),
    )
    assert any(row.name == "hop2-cookie" for row in page.items), (
        "the cookie-originated turn did not mint the token"
    )


# --- Review round 1 part 2 (the MCP branch of the origin rule) ------------
#
# A bearer MCP client's call_tool carries no ToolContext (the MCP endpoint
# hands handlers none), so the origin comes from the MCP request's live
# actor (the current_actor fallback branch of _originating_principal_ref).
# Before the branch existed, that origin was unknown: the trusted path
# rebuilt a "user" actor, the cookie-only rules passed, and a bearer MCP
# client could mint a token whose plaintext lands in the transcript it can
# read.


from mcp.types import CallToolRequestParams  # noqa: E402

from primer.auth.api_tokens import (  # noqa: E402
    extract_prefix,
    hash_token,
    mint_plaintext,
)
from primer.mcp.server import build_mcp_server, current_actor  # noqa: E402


class _GeneratedOnlyRegistry:
    """The MCP deps' provider registry, backed by the app's real generated
    route toolsets (the walk asks for toolsets by reserved id)."""

    def __init__(self, providers) -> None:
        self._providers = providers

    async def get_toolset(self, toolset_id: str):
        return self._providers.get(toolset_id)

    async def toolsets_off_by_design(self):
        return frozenset()


def _mcp_server_for(app):
    from primer.mcp.exposure import ExposureDeps

    deps = ExposureDeps(
        storage_provider=app.state.storage_provider,
        provider_registry=_GeneratedOnlyRegistry(
            dict(app.state.generated_route_toolsets.providers)
        ),
    )
    return build_mcp_server(lambda: deps)


def _actor_ref_bearer() -> PrincipalRef:
    return PrincipalRef(
        type="api_token",
        id="at-mcp-43",
        display="at-mcp-43",
        role="user",
        source="local",
        user_id="user-mcp",
    )


async def _mcp_call(server, actor, tool_name: str, arguments: dict):
    """One MCP tools/call as ``actor``: the server's own handler, the same
    seam the wire endpoint runs."""
    from primer.model.principal import Principal

    principal = Principal(
        type="api_token",
        id=actor.id,
        display=actor.display,
        role=actor.role,
        source="local",
        user_id=actor.user_id,
    )
    token = current_actor.set(principal)
    try:
        return await server.get_request_handler("tools/call").handler(
            None, CallToolRequestParams(name="call_tool", arguments={
                "scoped_id": tool_name, "arguments": arguments,
            })
        )
    finally:
        current_actor.reset(token)


@pytest.mark.asyncio
async def test_mcp_bearer_call_tool_cannot_mint_tokens(app, fake_storage_provider):
    """The MCP branch of the origin rule: a bearer MCP client's call_tool
    of the generated ``auth__create_api_token`` is refused by the
    cookie-only guard (its origin is the api_token the MCP actor names),
    and the same client can still call a non-cookie-only tool."""
    sp = fake_storage_provider
    await sp.get_storage(User).create(
        User(
            id="user-mcp",
            username="mcpowner",
            created_at=datetime.now(timezone.utc),
            role="user",
        )
    )
    plaintext = mint_plaintext()
    await sp.get_storage(ApiToken).create(
        ApiToken(
            id="at-mcp-43",
            user_id="user-mcp",
            name="mcp43",
            token_hash=hash_token(plaintext),
            prefix=extract_prefix(plaintext),
            scopes=["mcp"],
            created_at=datetime.now(timezone.utc),
        )
    )
    # The MCP endpoint's master switch is off by default: the test drives
    # the call path the endpoint runs, so the switch is on (rbac mode).
    from primer.model.mcp_exposure import McpExposure

    await sp.get_storage(McpExposure).create(
        McpExposure(
            enabled=True,
            mode="rbac",
            updated_at=datetime.now(timezone.utc),
        )
    )

    server = _mcp_server_for(app)
    actor = _actor_ref_bearer()

    out = await _mcp_call(
        server, actor, "auth__create_api_token",
        {"body": {"name": "mcp-leak"}},
    )
    assert out.is_error is True, (
        f"the bearer MCP client minted through call_tool (result: {out!r})"
    )
    assert "token_minting_forbidden" in out.content[0].text, (
        f"the refusal did not come from the cookie-only guard "
        f"(text: {out.content[0].text!r})"
    )
    page = await sp.get_storage(ApiToken).find(
        Q(ApiToken).where("user_id", "user-mcp").build(),
        OffsetPage(offset=0, length=200),
    )
    assert not any(row.name == "mcp-leak" for row in page.items), (
        "a bearer MCP call_tool minted an api token"
    )

    # Benign: the same bearer client's call of a non-cookie-only tool
    # (the exposure read) still works through the same path.
    got = await _mcp_call(server, actor, "mcp__get_exposure", {})
    assert got.is_error is False, (
        f"the benign bearer MCP call was refused (result: {got!r})"
    )
    got_outer = json.loads(got.content[0].text)
    assert got_outer.get("ok") is True, got_outer
    # The route tool wraps its body: the exposure JSON (with "enabled") is
    # the value of ``output``.
    assert '"enabled"' in got_outer.get("output", ""), got_outer
