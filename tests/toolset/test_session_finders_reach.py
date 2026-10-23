"""``find_sessions`` and ``list_workspace_sessions`` called from a session see only what that session may reach (lead ruling 3 of the G7 fixes, G7-05).

A session outside the default workspace reaches only its own workspace; a session in the default workspace (prime, the management plane) reaches
every workspace; a call with no calling session (a person over MCP, an API client) keeps its reach over every workspace. Before this, any agent could
list every session id of every workspace with either tool and then steer one, so "out of reach answers not-found, an id is never confirmed" was not a
boundary. Out of reach now answers exactly as an unknown id does: ``find_sessions`` finds nothing, ``list_workspace_sessions`` answers the
registry's own not-found. Calls go through ``dispatch_inner_tool``, as ``system__call_tool`` reaches the tools.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.api.registries import WorkspaceRegistry
from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from tests.session._peer_fakes import make_world, seed_session

T0 = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
USER = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local")


class _Registry:
    """``ws-1``, ``ws-2`` and ``ws-prime`` are live (each lists no slot sessions); any other id goes to a real ``WorkspaceRegistry`` that has no
    rows, so an unknown workspace answers in the production registry's own words."""

    def __init__(self, sp) -> None:
        self._real = WorkspaceRegistry(sp)

    async def get_workspace(self, workspace_id: str):
        if workspace_id in ("ws-1", "ws-2", "ws-prime"):
            return _Live()
        return await self._real.get_workspace(workspace_id)


class _Live:
    async def list_sessions(self):
        return []


async def _world():
    world = make_world()
    await world.sp.set_default_workspace_id("ws-prime")
    await seed_session(world, sid="ses-a", workspace_id="ws-1", initiated_by=USER, created_at=T0)
    await seed_session(world, sid="ses-b", workspace_id="ws-1", created_at=T0 + timedelta(minutes=1))
    await seed_session(world, sid="ses-x", workspace_id="ws-2", created_at=T0 + timedelta(minutes=2))
    await seed_session(world, sid="ses-p", workspace_id="ws-prime", initiated_by=USER, created_at=T0 + timedelta(minutes=3))
    return world


def _ctx(session_id: str, workspace_id: str) -> ToolContext:
    return ToolContext(tool_call_id="c1", session_id=session_id, workspace_id=workspace_id, initiated_by=USER)


async def _call(world, tool: str, arguments: dict, ctx: ToolContext | None):
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=_Registry(world.sp), scheduler=world.scheduler, claim_engine=world.engine,
    )
    result = await dispatch_inner_tool(provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=tool, arguments=arguments, ctx=ctx)
    return result, json.loads(result.output)


def _ids(body: dict) -> list[str]:
    return [item["id"] for item in body["items"]]


# ---- find_sessions -------------------------------------------------------------------------------------------------------------------


async def test_find_sessions_from_a_session_outside_the_default_workspace_finds_only_its_own_workspace():
    world = await _world()
    _, body = await _call(world, "find_sessions", {}, _ctx("ses-a", "ws-1"))
    assert _ids(body) == ["ses-b", "ses-a"]


async def test_find_sessions_naming_another_workspace_answers_as_a_workspace_that_does_not_exist():
    world = await _world()
    _, other = await _call(world, "find_sessions", {"workspace_id": "ws-2"}, _ctx("ses-a", "ws-1"))
    _, unknown = await _call(world, "find_sessions", {"workspace_id": "ws-nope"}, _ctx("ses-a", "ws-1"))
    assert other == unknown and other["items"] == []


@pytest.mark.parametrize(
    "ctx",
    [pytest.param(_ctx("ses-p", "ws-prime"), id="a session in the default workspace"), pytest.param(None, id="no calling session (MCP)")],
)
async def test_find_sessions_from_prime_or_with_no_session_finds_every_workspace(ctx):
    world = await _world()
    _, body = await _call(world, "find_sessions", {}, ctx)
    assert _ids(body) == ["ses-p", "ses-x", "ses-b", "ses-a"]
    _, body = await _call(world, "find_sessions", {"workspace_id": "ws-2"}, ctx)
    assert _ids(body) == ["ses-x"]


# ---- list_workspace_sessions ---------------------------------------------------------------------------------------------------------


async def test_list_workspace_sessions_of_another_workspace_from_a_session_answers_as_an_unknown_workspace():
    world = await _world()
    result, other = await _call(world, "list_workspace_sessions", {"workspace_id": "ws-2"}, _ctx("ses-a", "ws-1"))
    _, unknown = await _call(world, "list_workspace_sessions", {"workspace_id": "ws-nope"}, _ctx("ses-a", "ws-1"))
    assert result.is_error
    assert other == {"type": "not-found", "message": "Workspace 'ws-2' does not exist"}
    assert unknown == {"type": "not-found", "message": "Workspace 'ws-nope' does not exist"}, "the unknown id's answer is the registry's"


@pytest.mark.parametrize(
    ("workspace_id", "ctx"),
    [
        pytest.param("ws-1", _ctx("ses-a", "ws-1"), id="its own workspace"),
        pytest.param("ws-2", _ctx("ses-p", "ws-prime"), id="from the default workspace"),
        pytest.param("ws-2", None, id="no calling session (MCP)"),
    ],
)
async def test_list_workspace_sessions_within_reach_lists(workspace_id, ctx):
    world = await _world()
    result, body = await _call(world, "list_workspace_sessions", {"workspace_id": workspace_id}, ctx)
    assert not result.is_error, result.output
    assert body["items"] == [] and body["total"] == 0
