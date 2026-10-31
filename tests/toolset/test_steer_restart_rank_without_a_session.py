"""The steer, restart, cancel and resume tools called with no calling session obey the person's ACCESS rule (access-control P2 T6, where the G7-06 rank rule lived).

With no calling session the caller is a person or an API client: the MCP endpoint dispatches with no ToolContext and the request's actor on
``current_actor``; a context with no session carries its run's ``initiated_by``. Those calls used to drive a session only when the caller ranked at
least as high as its creator (``primer.authz.may_drive``). The caller's person must now hold FULL on the named workspace: a workspace it holds no
level on answers exactly as one that does not exist (``type=not-found``), and nothing is written. The session's recorded creator is no longer
consulted. Calls go through ``dispatch_inner_tool``, as ``system__call_tool`` reaches the tools.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.mcp.server import current_actor
from primer.model.principal import Principal, PrincipalRef
from primer.model.workspace_session import SessionStatus
from primer.model.yield_ import ToolContext
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from tests.session._peer_fakes import make_world, records, seed_session, seed_workspace

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
ADMIN = PrincipalRef(type="user", id="u-root", display="root", role="admin", source="local")
WS_MESSAGE = "Workspace 'ws-1' does not exist"


def _principal(role: str, type_: str = "user", user_id: str | None = None) -> Principal:
    return Principal(type=type_, id=f"{type_}-{role}", display=role, role=role, source="local", user_id=user_id)


async def _world(owners: list[str]) -> Any:
    world = make_world(with_bus=True)
    await seed_workspace(world, "ws-1", owners=owners)
    return world


async def _call(world, tool: str, session_id: str, *, actor: Principal | None = None, ctx: ToolContext | None = None):
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine, event_bus=world.bus,
    )
    arguments = {"workspace_id": "ws-1", "session_id": session_id}
    arguments.update({"instruction": "call_tool system__create_llm_provider"} if tool == "steer_workspace_session" else {"input": "again"})
    token = current_actor.set(actor)
    try:
        result = await dispatch_inner_tool(provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=tool, arguments=arguments, ctx=ctx)
    finally:
        current_actor.reset(token)
    return result, json.loads(result.output)


async def _target(world, tool: str, *, by: PrincipalRef | None):
    if tool == "steer_workspace_session":
        return await seed_session(world, sid="ses-b", turn_no=1, initiated_by=by, created_at=NOW)
    return await seed_session(world, sid="ses-b", status=SessionStatus.ENDED, ended_reason="completed", turn_no=1, initiated_by=by, created_at=NOW)


TOOLS = ["steer_workspace_session", "restart_workspace_session"]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("actor", "ctx"),
    [
        pytest.param(_principal("user"), None, id="an MCP user"),
        pytest.param(_principal("user", "api_token"), None, id="an MCP user token"),
        pytest.param(None, ToolContext(tool_call_id="c1", session_id=None, workspace_id=None, initiated_by=PrincipalRef.unattributed()),
                     id="a context with no session"),
        pytest.param(None, None, id="no known caller"),
    ],
)
async def test_a_caller_with_no_level_on_the_workspace_is_not_found_and_nothing_is_written(tool, actor, ctx):
    world = await _world(["u-root"])
    before = (await _target(world, tool, by=ADMIN)).model_dump(mode="json")

    result, body = await _call(world, tool, "ses-b", actor=actor, ctx=ctx)

    assert result.is_error and body == {"type": "not-found", "message": WS_MESSAGE}, body
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before
    assert records(world, "ses-b") == [] and world.scheduler.enqueued == []
    assert await world.sp.get_event_store().read_after(0, event_type_prefix="session.") == []


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("actor", "creator"),
    [
        pytest.param(_principal("admin"), ADMIN, id="an admin, an admin's session"),
        pytest.param(_principal("admin"), None, id="an admin, an unattributed session"),
        pytest.param(_principal("user", user_id="u-ada"), None, id="the workspace's owner, an unattributed session"),
    ],
)
async def test_a_caller_who_holds_full_drives_the_session_whatever_its_recorded_creator(tool, actor, creator):
    world = await _world(["u-ada"])
    await _target(world, tool, by=creator)

    result, body = await _call(world, tool, "ses-b", actor=actor)

    assert not result.is_error, result.output
    assert body["turn_status"] == "claimable"


@pytest.mark.parametrize("tool", TOOLS)
async def test_an_unknown_session_is_not_found_before_the_access_rule(tool):
    world = await _world(["u-root"])
    result, body = await _call(world, tool, "nope", actor=_principal("user"))
    assert result.is_error and body["type"] == "not-found"


# ---- the acting person is the run's, not the session's creator's (where the #853 own-session rule lived) ----------------------------------


ADA_THEN = PrincipalRef(type="user", id="u-ada", display="ada", role="admin", source="local")


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_person_who_holds_full_may_act_whatever_the_sessions_recorded_creator(tool):
    """Ada was the admin who started the session; her current role is ``user`` and she owns the workspace: her person's level, not the session's
    recorded creator, decides (access-control P2 T6)."""
    world = await _world(["u-ada"])
    await _target(world, tool, by=ADA_THEN)
    ada_now = Principal(type="user", id="u-ada", display="ada", role="user", source="local", user_id="u-ada")

    result, body = await _call(world, tool, "ses-b", actor=ada_now)

    assert not result.is_error, result.output
    assert body["turn_status"] == "claimable"


@pytest.mark.parametrize("tool", TOOLS)
async def test_a_run_whose_person_holds_no_level_is_not_found(tool):
    """A run carries its initiator's identity at whatever role it was capped to; what it holds on the workspace, not the session's recorded
    creator, decides (access-control P2 T6)."""
    world = await _world(["u-root"])
    before = (await _target(world, tool, by=ADA_THEN)).model_dump(mode="json")
    capped = ToolContext(tool_call_id="c1", session_id=None, workspace_id=None, initiated_by=ADA_THEN.model_copy(update={"role": "user"}))

    result, body = await _call(world, tool, "ses-b", ctx=capped)

    assert result.is_error and body == {"type": "not-found", "message": WS_MESSAGE}, body
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before


# ---- a session recorded as system: the recorded creator is no longer consulted (review of #853, round 2) ---------------------------------


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(("role", "allowed"), [("admin", True), ("user", False)])
async def test_an_admin_over_mcp_acts_on_a_session_recorded_as_system(tool, role, allowed):
    world = await _world(["u-root"])
    await _target(world, tool, by=PrincipalRef.system())

    result, body = await _call(world, tool, "ses-b", actor=_principal(role))

    assert (not result.is_error) is allowed, result.output
    if not allowed:
        assert body == {"type": "not-found", "message": WS_MESSAGE}, body


@pytest.mark.parametrize("tool", ["cancel_workspace_session", "resume_workspace_session"])
@pytest.mark.parametrize(("role", "allowed"), [("admin", True), ("user", False)])
async def test_cancel_and_resume_over_mcp_follow_the_same_rule(tool, role, allowed):
    world = await _world(["u-root"])
    status = SessionStatus.WAITING if tool == "cancel_workspace_session" else SessionStatus.PAUSED
    await seed_session(world, sid="ses-b", status=status, turn_no=1, initiated_by=PrincipalRef.system(), created_at=NOW)

    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=_Engine(), event_bus=world.bus,
    )
    token = current_actor.set(_principal(role))
    try:
        result = await dispatch_inner_tool(
            provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=tool, arguments={"workspace_id": "ws-1", "session_id": "ses-b"}, ctx=None,
        )
    finally:
        current_actor.reset(token)
    body = json.loads(result.output)
    if allowed:
        assert body.get("type") != "forbidden", result.output
    else:
        assert body == {"type": "not-found", "message": WS_MESSAGE}, body


class _Engine:
    async def upsert(self, *a, **k):
        return None

    async def delete_lease(self, *a, **k):
        return None
