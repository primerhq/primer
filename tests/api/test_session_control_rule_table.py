"""The rule table's rows for the paths that act on a session without putting input into it (review of #853, the lead's EXTEND ruling).

A session outside the default workspace reaches only its own workspace, for every workspaces tool that names a session or a workspace: the cancel,
get, read-messages, pause, resume, interrupt and create tools (an agent that could create a session in prime would reach everything from there). An
out-of-reach target answers exactly as an unknown id does. Cancel and resume also follow the rank rule (``primer.authz.may_drive``) from a session,
over MCP and on their REST routes: they stop or start a run at the creator's rank. The drive rows are pinned by
``tests/api/test_session_drive_rule_table.py``; these are the rest of ``docs/dev/subsystems/sessions.md`` section 9's table.

The tool rows go through :func:`primer.agent.inner_dispatch.dispatch_inner_tool`, as ``system__call_tool`` reaches a tool (with the request's actor
on ``current_actor`` and no context for the MCP rows); the REST rows go over HTTP through the auth middleware as a user-role account.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.mcp.server import current_actor
from primer.model.agent import Agent, AgentModel
from primer.model.principal import Principal, PrincipalRef
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionInfo, SessionStatus
from primer.model.yield_ import ToolContext
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from tests.session._peer_fakes import FakeBus, FakeEngine, FakeRegistry, FakeScheduler, FakeSlot, FakeWorkspace, World, seed_session
from tests.conftest import _FakeStorageProvider

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
USER = PrincipalRef(type="user", id="u-user", display="user", role="user", source="local")
ADMIN = PrincipalRef(type="user", id="u-root", display="root", role="admin", source="local")
D, F, N = "delivered", "forbidden", "not-found"
P = "from prime"

#: path -> (target status, what each scenario answers). ``outranked``: the target was started by an admin, the caller ranks user; ``same rank``: by a
#: user; ``out of reach``: the target is in another workspace than the calling session's (not the default one); ``from prime``: the calling session
#: is in the default workspace and the target in another.
TABLE: dict[str, tuple[SessionStatus, dict[str, str]]] = {
    "cancel tool, in a session":        (SessionStatus.WAITING, {"outranked": F, "same rank": D, "out of reach": N, P: D}),
    "cancel tool, no session":          (SessionStatus.WAITING, {"outranked": F, "same rank": D, "out of reach": D, P: D}),
    "REST cancel":                      (SessionStatus.WAITING, {"outranked": F, "same rank": D, "out of reach": D, P: D}),
    "resume tool, in a session":        (SessionStatus.PAUSED, {"outranked": F, "same rank": D, "out of reach": N, P: D}),
    "resume tool, no session":          (SessionStatus.PAUSED, {"outranked": F, "same rank": D, "out of reach": D, P: D}),
    "REST resume":                      (SessionStatus.PAUSED, {"outranked": F, "same rank": D, "out of reach": D, P: D}),
    "pause tool, in a session":         (SessionStatus.WAITING, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
    "interrupt tool, in a session":     (SessionStatus.WAITING, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
    "get session tool, in a session":   (SessionStatus.WAITING, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
    "read messages tool, in a session": (SessionStatus.WAITING, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
}
SCENARIOS = ["outranked", "same rank", "out of reach", P]
TOOLS = {
    "cancel": "cancel_workspace_session", "resume": "resume_workspace_session", "pause": "pause_workspace_session",
    "interrupt": "interrupt_workspace_session", "get session": "get_workspace_session", "read messages": "read_workspace_session_messages",
}


class _Slot(FakeSlot):
    """The on-disk slot, as the pause, resume and get tools use it."""

    def __init__(self, session_id: str = "ses-t") -> None:
        super().__init__()
        self.session_id = session_id
        self.calls: list[str] = []

    async def request_pause(self) -> None:
        self.calls.append("request_pause")

    async def request_resume(self) -> None:
        self.calls.append("request_resume")

    async def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id, agent_id="agent-ses-t", workspace_id="ws-2", status=SessionStatus.WAITING, started_at=NOW,
            last_activity_at=NOW,
        )


class _Workspace(FakeWorkspace):
    state_path = ".state"

    def slot(self, session_id: str) -> _Slot:
        return self.slots.setdefault(session_id, _Slot(session_id))  # type: ignore[return-value]

    async def read_file(self, path: str) -> bytes:
        return b""


class _Registry(FakeRegistry):
    def workspace(self, workspace_id: str) -> _Workspace:
        return self.workspaces.setdefault(workspace_id, _Workspace())  # type: ignore[return-value]


@dataclass
class Env:
    world: World
    rest: httpx.AsyncClient | None = None
    target_workspace: str = "ws-1"
    closers: list = field(default_factory=list)


async def _seed(world: World, path: str, scenario: str) -> str:
    if scenario == P:
        await world.sp.set_default_workspace_id("ws-1")
    await seed_session(world, sid="ses-a", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER, created_at=NOW)
    workspace = "ws-2" if scenario in ("out of reach", P) else "ws-1"
    await seed_session(
        world, sid="ses-t", workspace_id=workspace, status=TABLE[path][0], turn_no=1, created_at=NOW,
        initiated_by=ADMIN if scenario == "outranked" else USER,
    )
    world.registry.remove("ws-nope")
    return workspace


def _world(sp=None) -> World:
    return World(sp=sp or _FakeStorageProvider(), registry=_Registry(), scheduler=FakeScheduler(), engine=_Engine(), bus=FakeBus())


class _Engine(FakeEngine):
    async def delete_lease(self, kind, session_id) -> None:
        return None


async def _tool(env: Env, path: str, *, workspace: str, session: str, in_session: bool) -> tuple[str, dict]:
    world = env.world
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine, event_bus=world.bus,
    )
    name = TOOLS[path.split(" tool,")[0]]
    arguments = {"workspace_id": workspace, "session_id": session}
    if in_session:
        ctx = ToolContext(tool_call_id="call-1", session_id="ses-a", workspace_id="ws-1", initiated_by=USER)
        result = await dispatch_inner_tool(provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=name, arguments=arguments, ctx=ctx)
    else:
        token = current_actor.set(Principal(type="user", id="u-mcp", display="mcp", role="user", source="local"))
        try:
            result = await dispatch_inner_tool(provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=name, arguments=arguments, ctx=None)
        finally:
            current_actor.reset(token)
    body = json.loads(result.output)
    return (D if not result.is_error else body["type"]), body


async def _drive(env: Env, path: str) -> tuple[str, dict]:
    if path.startswith("REST"):
        verb = "cancel" if path == "REST cancel" else "resume"
        r = await env.rest.post(f"/v1/workspaces/{env.target_workspace}/sessions/ses-t/{verb}", json={})
        return {200: D, 403: F, 404: N}[r.status_code], r.json()
    return await _tool(env, path, workspace=env.target_workspace, session="ses-t", in_session=path.endswith("in a session"))


async def _delivered(env: Env, path: str) -> bool:
    row = await env.world.sessions.get("ses-t")
    slot = env.world.registry.workspace(env.target_workspace).slot("ses-t")
    if path.startswith(("cancel", "REST cancel")):
        return (row.status, row.ended_reason) == (SessionStatus.ENDED, "cancelled")
    if path == "REST resume":
        return row.status == SessionStatus.RUNNING
    if path.startswith("resume"):
        return slot.calls == ["request_resume"]
    if path.startswith("pause"):
        return slot.calls == ["request_pause"]
    return True  # interrupt, get, read: the answer is the delivery


async def _check(env: Env, path: str, scenario: str) -> None:
    before = (await env.world.sessions.get("ses-t")).model_dump(mode="json")

    outcome, body = await _drive(env, path)

    assert outcome == TABLE[path][1][scenario], (path, scenario, body)
    if outcome == D:
        assert await _delivered(env, path), (path, scenario)
        return
    assert (await env.world.sessions.get("ses-t")).model_dump(mode="json") == before, f"{path} wrote to the target it refused ({scenario})"
    assert env.world.registry.workspace(env.target_workspace).slot("ses-t").calls == []
    if outcome == N:
        # The out-of-reach answer is exactly what an unknown workspace and session answer (asked with no calling session, past every reach rule).
        _, unknown = await _tool(env, path, workspace="ws-nope", session="ses-nobody", in_session=False)
        seen = json.dumps(body).replace("ws-2", "<ws>").replace("ses-t", "<id>")
        assert seen == json.dumps(unknown).replace("ws-nope", "<ws>").replace("ses-nobody", "<id>"), (body, unknown)


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("path", [p for p in TABLE if not p.startswith("REST")])
async def test_every_tool_that_acts_on_a_session_obeys_the_rule_table(path, scenario):
    env = Env(world=_world())
    env.target_workspace = await _seed(env.world, path, scenario)
    await _check(env, path, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("path", [p for p in TABLE if p.startswith("REST")])
async def test_the_rest_cancel_and_resume_obey_the_rule_table(app, client, fake_storage_provider, path, scenario):
    world = _world(fake_storage_provider)
    app.state.workspace_registry.get_workspace = world.registry.get_workspace  # type: ignore[method-assign]
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
        assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
        env = Env(world=world, rest=carol)
        env.target_workspace = await _seed(world, path, scenario)
        await _check(env, path, scenario)


# ---- create_workspace_session ------------------------------------------------------------------------------------------------------


async def _create(sp, workspace: str, ctx: ToolContext | None):
    world = _world(sp)
    toolset = build_workspaces_toolset(
        storage_provider=sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine, event_bus=world.bus,
    )
    result = await dispatch_inner_tool(
        provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name="create_workspace_session",
        arguments={"workspace_id": workspace, "binding": {"kind": "agent", "agent_id": "nobody"}}, ctx=ctx,
    )
    return json.loads(result.output)


@pytest.mark.parametrize(("own", "target", "reached"), [
    pytest.param("ws-1", "ws-2", False, id="another workspace from a session outside prime"),
    pytest.param("ws-1", "ws-1", True, id="its own workspace"),
    pytest.param("ws-prime", "ws-2", True, id="another workspace from prime"),
])
async def test_create_workspace_session_from_a_session_reaches_only_what_the_session_reaches(own, target, reached):
    """A session outside prime that could create a session in prime would reach every workspace from there. Out of reach answers exactly as a
    workspace that does not exist; within reach the create goes on (here it stops at the agent it names, which does not exist)."""
    sp = _FakeStorageProvider()
    await sp.set_default_workspace_id("ws-prime")
    for wid in ("ws-1", "ws-2", "ws-prime"):
        await sp.get_storage(Workspace).create(Workspace(
            id=wid, name=wid, template_id="local-default", provider_id="local", created_at=NOW, phase="running",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
        ))
    await sp.get_storage(Agent).create(Agent(id="somebody", description="x", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]))
    ctx = ToolContext(tool_call_id="c1", session_id="ses-a", workspace_id=own, initiated_by=USER)

    body = await _create(sp, target, ctx)
    unknown = await _create(sp, "ws-nope", None)

    assert unknown == {"type": "not-found", "message": "Workspace 'ws-nope' does not exist"}
    if reached:
        assert body["type"] != "not-found", body
    else:
        assert body == {"type": "not-found", "message": f"Workspace {target!r} does not exist"}


@pytest.mark.parametrize("tool", ["cancel_workspace_session", "resume_workspace_session"])
async def test_cancel_and_resume_from_a_session_are_refused_in_the_words_of_a_message_steer_or_restart(tool):
    """One refusal text for a run (review of #853, round 2): the rank refusal of a message, a steer and a restart from a session."""
    from primer.session.peer_messages import REFUSE_RANK

    env = Env(world=_world())
    env.target_workspace = await _seed(env.world, "cancel tool, in a session" if tool.startswith("cancel") else "resume tool, in a session", "outranked")
    path = "cancel tool, in a session" if tool.startswith("cancel") else "resume tool, in a session"

    outcome, body = await _tool(env, path, workspace="ws-1", session="ses-t", in_session=True)

    assert (outcome, body["message"]) == (F, REFUSE_RANK.format(session_id="ses-t", target_rank="admin", sender_rank="user"))
