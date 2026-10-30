"""``find_sessions`` and ``list_workspace_sessions`` see only the workspaces the calling person can read (access-control P2 T6, where the G7-05 reach rule lived).

The G7-05 rule keyed visibility on WHICH WORKSPACE the calling session sat in (from prime everything, from elsewhere only its own). P2 T6 keys it on the person behind
the call and the level that person holds: a run finds the sessions of the workspaces its person reads, a person or an API client (no calling session, the MCP endpoint)
is checked as the token's user, and a workspace the person holds no level on answers exactly as one that does not exist. Out of access now answers exactly as an
unknown id does: ``find_sessions`` finds nothing, ``list_workspace_sessions`` answers the access gate's own not-found. Calls go through ``dispatch_inner_tool``, as
``system__call_tool`` reaches the tools.
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
from tests.session._peer_fakes import make_world, seed_session, seed_workspace

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


async def _world(*, ws2: dict | None = None, prime: dict | None = None):
    """The four sessions over three workspaces: u1 owns ws-1 (the run's person, ``USER``), u2 owns the other two, so u1 holds no level on them
    unless the caller adds a grant or opens the row (access-control P2 T6)."""
    world = make_world()
    await world.sp.set_default_workspace_id("ws-prime")
    await seed_workspace(world, "ws-1", owners=["u1"])
    await seed_workspace(world, "ws-2", owners=["u2"], **(ws2 or {}))
    await seed_workspace(world, "ws-prime", owners=["u2"], **(prime or {}))
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


async def test_find_sessions_spans_only_the_workspaces_the_run_s_person_can_read():
    world = await _world()
    _, body = await _call(world, "find_sessions", {}, _ctx("ses-a", "ws-1"))
    assert _ids(body) == ["ses-b", "ses-a"]


async def test_find_sessions_naming_another_workspace_answers_as_a_workspace_that_does_not_exist():
    """A workspace the person holds no level on answers the access gate's not-found: the same type and empty page as a workspace that does
    not exist, with the named id in the message (access-control P2 T6)."""
    world = await _world()
    _, other = await _call(world, "find_sessions", {"workspace_id": "ws-2"}, _ctx("ses-a", "ws-1"))
    _, unknown = await _call(world, "find_sessions", {"workspace_id": "ws-nope"}, _ctx("ses-a", "ws-1"))
    assert other == {"type": "not-found", "message": "Workspace 'ws-2' does not exist"}, other
    assert unknown == {"type": "not-found", "message": "Workspace 'ws-nope' does not exist"}, unknown


async def test_find_sessions_from_a_person_who_reads_every_workspace_finds_them_all():
    """Where the old 'from prime reaches everything' rule lived: a person holding read on every workspace finds every session, whatever
    workspace the calling session sits in (access-control P2 T6)."""
    world = await _world(ws2={"grants": {"u1": "read"}}, prime={"grants": {"u1": "read"}})
    _, body = await _call(world, "find_sessions", {}, _ctx("ses-p", "ws-prime"))
    assert _ids(body) == ["ses-p", "ses-x", "ses-b", "ses-a"]
    _, body = await _call(world, "find_sessions", {"workspace_id": "ws-2"}, _ctx("ses-p", "ws-prime"))
    assert _ids(body) == ["ses-x"]


async def test_find_sessions_with_no_session_finds_only_the_public_workspaces():
    """Where the old 'MCP reaches everything' rule lived: a call with no calling session (the MCP endpoint, no actor) acts as an unknown
    person and reads only the public workspaces (access-control P2 T6)."""
    world = await _world(ws2={"public": True})
    _, body = await _call(world, "find_sessions", {}, None)
    assert _ids(body) == ["ses-x"]


# ---- list_workspace_sessions ---------------------------------------------------------------------------------------------------------


async def test_list_workspace_sessions_of_another_workspace_from_a_session_answers_as_an_unknown_workspace():
    world = await _world()
    result, other = await _call(world, "list_workspace_sessions", {"workspace_id": "ws-2"}, _ctx("ses-a", "ws-1"))
    _, unknown = await _call(world, "list_workspace_sessions", {"workspace_id": "ws-nope"}, _ctx("ses-a", "ws-1"))
    assert result.is_error
    assert other == {"type": "not-found", "message": "Workspace 'ws-2' does not exist"}
    assert unknown == {"type": "not-found", "message": "Workspace 'ws-nope' does not exist"}, "the unknown id's answer is the same not-found"


@pytest.mark.parametrize(
    ("workspace_id", "ctx", "world_kwargs"),
    [
        pytest.param("ws-1", _ctx("ses-a", "ws-1"), {}, id="the run owns the workspace"),
        pytest.param("ws-2", _ctx("ses-p", "ws-prime"), {"ws2": {"grants": {"u1": "read"}}}, id="a read grant on the target"),
        pytest.param("ws-2", None, {"ws2": {"public": True}}, id="no calling session (MCP), the target is public"),
    ],
)
async def test_list_workspace_sessions_the_person_can_read_lists(workspace_id, ctx, world_kwargs):
    world = await _world(**world_kwargs)
    result, body = await _call(world, "list_workspace_sessions", {"workspace_id": workspace_id}, ctx)
    assert not result.is_error, result.output
    assert body["items"] == [] and body["total"] == 0
