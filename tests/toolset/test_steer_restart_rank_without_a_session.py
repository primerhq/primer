"""The steer and restart tools called with no calling session obey the person's rank rule (lead ruling 2 of the G7 fixes, audit G7-06).

With no calling session the caller is a person or an API client: the MCP endpoint dispatches with no ToolContext and the request's actor on
``current_actor``; a context with no session carries its run's ``initiated_by``. Both tools used to drive any session at any rank. The caller must now
rank at least as high as the session's creator (``primer.authz.may_drive``), or the call answers ``type=forbidden`` with a fixed message that names
neither the creator nor its rank, and nothing is written. Calls go through ``dispatch_inner_tool``, as ``system__call_tool`` reaches the tools.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.authz import OUTRANKED_MESSAGE
from primer.mcp.server import current_actor
from primer.model.principal import Principal, PrincipalRef
from primer.model.workspace_session import SessionStatus
from primer.model.yield_ import ToolContext
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from tests.session._peer_fakes import make_world, records, seed_session

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
ADMIN = PrincipalRef(type="user", id="u-root", display="root", role="admin", source="local")


def _principal(role: str, type_: str = "user") -> Principal:
    return Principal(type=type_, id=f"{type_}-{role}", display=role, role=role, source="local")


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
    ],
)
async def test_a_caller_below_the_sessions_creator_is_forbidden_and_nothing_is_written(tool, actor, ctx):
    world = make_world(with_bus=True)
    before = (await _target(world, tool, by=ADMIN)).model_dump(mode="json")

    result, body = await _call(world, tool, "ses-b", actor=actor, ctx=ctx)

    assert result.is_error and body == {"type": "forbidden", "message": OUTRANKED_MESSAGE}
    assert (await world.sessions.get("ses-b")).model_dump(mode="json") == before
    assert records(world, "ses-b") == [] and world.scheduler.enqueued == []
    assert await world.sp.get_event_store().read_after(0, event_type_prefix="session.") == []


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    ("actor", "creator"),
    [
        pytest.param(_principal("admin"), ADMIN, id="an admin, an admin's session"),
        pytest.param(_principal("admin"), None, id="an admin, an unattributed session"),
        pytest.param(_principal("user"), None, id="a user, an unattributed session"),
        pytest.param(None, None, id="no known caller (ranks as a user), an unattributed session"),
    ],
)
async def test_a_caller_at_or_above_the_sessions_creator_drives_it(tool, actor, creator):
    world = make_world(with_bus=True)
    await _target(world, tool, by=creator)

    result, body = await _call(world, tool, "ses-b", actor=actor)

    assert not result.is_error, result.output
    assert body["turn_status"] == "claimable"


@pytest.mark.parametrize("tool", TOOLS)
async def test_an_unknown_session_is_not_found_before_the_rank_rule(tool):
    world = make_world(with_bus=True)
    result, body = await _call(world, tool, "nope", actor=_principal("user"))
    assert result.is_error and body["type"] == "not-found"
