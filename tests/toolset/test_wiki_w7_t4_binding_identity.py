"""The native MCP binding save retains the same caller as trigger subscriptions."""

from datetime import UTC, datetime
import json

import pytest

from primer.agent.invoke import _RUN_ID
from primer.mcp.server import current_actor
from primer.model.principal import Principal, PrincipalRef
from primer.model.access import Access
from primer.model.trigger import Subscription
from primer.model.workspace import Workspace
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.toolset.trigger import _caller
from tests.toolset.test_system_channel_binding_tools import (
    _make_workspace,
    _seed_channel_trigger,
    pr as pr,
    sp as sp,
    system_toolset as system_toolset,
)


async def seed(sp):
    await _seed_channel_trigger(sp)
    await sp.get_storage(Workspace).create(_make_workspace("ws-1", owner="u-owner"))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="sess-1", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="ag-1"),
        status=SessionStatus.RUNNING, created_at=datetime.now(UTC),
    ))
    return {"trigger_id": "trg-ch-1", "config": {"kind": "session_append", "session_id": "sess-1"}}


@pytest.mark.parametrize("kind", ["user", "api_token"])
async def test_native_mcp_channel_binding_uses_the_authenticated_user_as_saved_owner(sp, system_toolset, kind):
    arguments = await seed(sp)
    actor = Principal(type=kind, id="u-owner" if kind == "user" else "tok-owner",
                      user_id="u-owner", display="owner", role="user", source="internal" if kind == "api_token" else "local")
    token = current_actor.set(actor)
    try:
        # invoke_one, the native MCP dispatch seam, supplies ctx=None.
        result = await system_toolset.call(tool_name="create_channel_binding", arguments=arguments, ctx=None)
    finally:
        current_actor.reset(token)
    assert not result.is_error, result.output
    body = json.loads(result.output)
    owner = body["owner"]
    assert owner["id"] == actor.id and owner["user_id"] == "u-owner" and owner["role"] == "user"
    assert owner["type"] == kind  # keep the token identity so revocation still applies at fire time
    stored = await sp.get_storage(Subscription).get(body["id"])
    assert stored.owner.model_dump(mode="json") == owner


async def test_explicit_worker_principal_wins_over_an_ambient_admin(sp, system_toolset):
    arguments = await seed(sp)
    owner = PrincipalRef(type="user", id="u-owner", user_id="u-owner", display="worker", role="user", source="local")
    ctx = ToolContext(tool_call_id="one", session_id=None, workspace_id=None, initiated_by=owner)
    token = current_actor.set(Principal(type="user", id="u-admin", user_id="u-admin", display="admin", role="admin", source="local"))
    try:
        result = await system_toolset.call(tool_name="create_channel_binding", arguments=arguments, ctx=ctx)
    finally:
        current_actor.reset(token)
    assert not result.is_error, result.output
    assert json.loads(result.output)["owner"] == owner.model_dump(mode="json")


@pytest.mark.parametrize("mode", ["no-actor", "delegated", "unattributed-context"])
async def test_unknown_or_delegated_call_cannot_borrow_the_request_actor(sp, system_toolset, mode):
    arguments = await seed(sp)
    actor = None if mode == "no-actor" else Principal(type="user", id="u-admin", user_id="u-admin", display="admin", role="admin", source="local")
    token = current_actor.set(actor)
    run_token = _RUN_ID.set("delegated" if mode == "delegated" else None)
    ctx = ToolContext(tool_call_id="one", session_id=None, workspace_id=None) if mode == "unattributed-context" else None
    try:
        assert _caller(ctx) is None
        result = await system_toolset.call(tool_name="create_channel_binding", arguments=arguments, ctx=ctx)
    finally:
        _RUN_ID.reset(run_token)
        current_actor.reset(token)
    assert result.is_error
    assert not sp.get_storage(Subscription)._data


async def test_read_only_native_caller_still_cannot_create_a_binding(sp, system_toolset):
    arguments = await seed(sp)
    token = current_actor.set(Principal(type="user", id="u-other", user_id="u-other", display="reader", role="user", source="local"))
    try:
        result = await system_toolset.call(tool_name="create_channel_binding", arguments=arguments, ctx=None)
    finally:
        current_actor.reset(token)
    assert result.is_error and json.loads(result.output)["type"] == "forbidden"
    assert not sp.get_storage(Subscription)._data


@pytest.mark.parametrize("target", ["missing", "inaccessible"])
async def test_native_binding_requires_the_actual_saved_owner_to_access_a_real_target(sp, system_toolset, target):
    arguments = await seed(sp)
    if target == "missing":
        arguments["config"]["session_id"] = "missing-session"
    else:
        workspace = await sp.get_storage(Workspace).get("ws-1")
        workspace.access = Access(owners=["someone-else"])
        await sp.get_storage(Workspace).update(workspace)
    token = current_actor.set(Principal(type="user", id="u-owner", user_id="u-owner", display="owner", role="user", source="local"))
    try:
        result = await system_toolset.call(tool_name="create_channel_binding", arguments=arguments, ctx=None)
    finally:
        current_actor.reset(token)
    assert result.is_error and json.loads(result.output)["type"] == "not-found"
    assert not sp.get_storage(Subscription)._data
