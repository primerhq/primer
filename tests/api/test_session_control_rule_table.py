"""The rule table's rows for the paths that act on a session without putting input into it (review of #853, the lead's EXTEND ruling).

A session outside the default workspace reaches only its own workspace, for every workspaces tool that names a session or a workspace: the cancel,
get, read-messages, pause, resume, interrupt and create tools (an agent that could create a session in prime would reach everything from there). An
out-of-reach target answers exactly as an unknown id does. Cancel and resume also follow the rank rule (``primer.authz.may_drive``) from a session,
over MCP and on their REST routes: they stop or start a run at the creator's rank. The drive rows are pinned by
``tests/api/test_session_drive_rule_table.py``; these are the rest of ``docs/dev/subsystems/sessions.md`` section 9's table.

The tool rows go through :func:`primer.agent.inner_dispatch.dispatch_inner_tool`, as ``system__call_tool`` reaches a tool (with the request's actor
on ``current_actor`` and no context for the MCP rows); the REST rows go over HTTP through the auth middleware as a user-role account.

The next section holds the rows of the tools that name a WORKSPACE rather than a session (ticket 01a16ad1): delete, get and list a workspace, its
tap, its log and its files. They follow the same reach rule and are driven through the real tool manager (by name and through ``system__call_tool``).

The last section holds the follow-ups of #870 (tickets 01a16ba1-da9b and 01a16ba1-e2d1): a trigger subscription or a channel binding (created,
updated, and fired by the worker's trigger handler), the reply-binding tools, ``delete_workspace``'s rank rule, ``create_workspace`` from a session
outside prime, and the tools of a graph session that has no holder slot.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.agent.tool_manager import ToolExecutionManager, invoke_one
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.authz import OUTRANKED_MESSAGE
from primer.claim.in_memory import InMemoryClaimEngine
from primer.mcp.server import current_actor
from primer.model.agent import Agent, AgentModel
from primer.model.channel import Channel, ChannelProviderType, TelegramChannelConfig
from primer.model.chat import ToolCallPart
from primer.model.except_ import NotFoundError
from primer.model.graph import Graph, _BeginNode, _EndNode, _StaticEdge
from primer.model.principal import Principal, PrincipalRef
from primer.model.scheduler import WorkerConfig
from primer.model.storage import OffsetPage
from primer.model.trigger import (
    AgentFreshSubConfig,
    ChannelTriggerConfig,
    DelayedTriggerConfig,
    GraphFreshSubConfig,
    ParkedSessionSubConfig,
    SessionAppendSubConfig,
    Subscription,
    Trigger,
)
from primer.model.user import User
from primer.model.workspace import (
    CommitInfo,
    FileEntry,
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceChannelLink,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
    WorkspaceTemplate,
)
from primer.model.workspace_session import AgentSessionBinding, GraphSessionBinding, SessionInfo, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.session.peer_messages import REFUSE_RANK
from primer.toolset.system import build_system_toolset
from primer.toolset.trigger import build_trigger_toolset_provider
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from primer.worker.engine_handlers import run_engine_trigger
from primer.worker.pool import WorkerPool
from primer.workspace.local.state import LocalStateRepo
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


# ---- The workspace tools (ticket 01a16ad1) ----------------------------------------------------------------------------------------------
#
# The tools that name a WORKSPACE: deleting it, reading its row, draining its tap, reading its log and its files, writing and deleting a file. Before
# this they took no context, so a session in any workspace reached every workspace through them (``system__call_tool`` is open to every agent).
# A session outside the default workspace now reaches only its own workspace with each of them; from prime, and with no calling session (MCP), every
# workspace is reachable. Every in-session row goes through the real tool manager twice: the tool by its own scoped name (an agent that lists the
# workspaces toolset), and ``system__call_tool`` (the door every agent has). The no-session row is the MCP endpoint's own call (``invoke_one`` with
# the request's actor on ``current_actor``).

W = "<target>"
#: tool -> its arguments, the target workspace written as ``W``.
WS_TOOLS: dict[str, dict] = {
    "delete_workspace": {"id": W},
    "get_workspace": {"id": W},
    "workspace_tap": {"workspace_id": W},
    "get_workspace_log": {"workspace_id": W},
    "list_workspace_files": {"workspace_id": W},
    "get_workspace_file_info": {"workspace_id": W, "path": "notes.txt"},
    "read_workspace_file": {"workspace_id": W, "path": "notes.txt"},
    "write_workspace_file": {"workspace_id": W, "path": "notes.txt", "content": "changed"},
    "delete_workspace_file": {"workspace_id": W, "path": "notes.txt"},
}
#: who calls -> (the calling session's workspace, or None for no calling session; what every tool answers). The target is always ``ws-2``.
WS_CALLERS: dict[str, tuple[str | None, str]] = {
    "out of reach": ("ws-1", N),
    "own workspace": ("ws-2", D),
    P: ("ws-prime", D),
    "no session": (None, D),
}
DOORS = ("tool manager", "system__call_tool")
_TRANSCRIPT = json.dumps({"seq": 1, "kind": "user_input", "payload": {"text": "hi"}, "created_at": NOW.isoformat()}).encode() + b"\n"


class _LiveWorkspace:
    """A live workspace: a file, the transcript of the session in it, and one commit in its state repo."""

    def __init__(self, workspace_id: str) -> None:
        self.id = workspace_id
        self.files = {"notes.txt": f"hello from {workspace_id}".encode(), ".state/sessions/ses-t/messages.jsonl": _TRANSCRIPT}
        self.runtime_meta = WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t")

    def _entry(self, path: str) -> FileEntry:
        if path not in self.files:
            raise NotFoundError(f"{path!r} not found")
        return FileEntry(path=path, kind="file", size_bytes=len(self.files[path]), modified_at=NOW)

    async def list_files(self, path: str = ".", *, recursive: bool = False, max_entries: int | None = None) -> list[FileEntry]:
        return [self._entry(p) for p in sorted(self.files)][:max_entries]

    async def file_info(self, path: str) -> FileEntry:
        return self._entry(path)

    async def read_file(self, path: str) -> bytes:
        return self.files[self._entry(path).path]

    async def write_file(self, path: str, content: bytes) -> None:
        self.files[path] = content

    async def start_session(self, binding, *, id: str, instructions=None, parent_session_id=None, name=None) -> None:  # noqa: A002
        self.files[f".state/sessions/{id}/messages.jsonl"] = b""

    async def append_message_line(self, session_id: str, line: bytes) -> None:
        path = f".state/sessions/{session_id}/messages.jsonl"
        self.files[path] = self.files.get(path, b"") + line

    async def delete_file(self, path: str, *, recursive: bool = False) -> None:
        del self.files[self._entry(path).path]

    async def log(self, *, limit: int = 50, with_files: bool = False) -> list[CommitInfo]:
        return [CommitInfo(sha="a" * 40, subject="init", committed_at=NOW, workspace_id=self.id)]


class _Backend:
    """The provider's backend, as the real ``WorkspaceRegistry`` reaches it: one live workspace per id."""

    def __init__(self) -> None:
        self.live = {wid: _LiveWorkspace(wid) for wid in ("ws-1", "ws-2", "ws-prime")}

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get(self, workspace_id: str, *, template=None) -> _LiveWorkspace | None:
        return self.live.get(workspace_id)

    async def create(self, template, *, overrides=None, workspace_id=None, resolvers=None) -> _LiveWorkspace:
        wid = workspace_id or "ws-made"
        self.live[wid] = _LiveWorkspace(wid)
        return self.live[wid]

    async def destroy(self, workspace_id: str) -> None:
        if self.live.pop(workspace_id, None) is None:
            raise NotFoundError(f"workspace {workspace_id!r} not found")


@dataclass
class WsWorld:
    sp: _FakeStorageProvider
    backend: _Backend
    toolset: object
    system: object
    trigger: object = None
    registry: ProviderRegistry | None = None
    workspaces: WorkspaceRegistry | None = None

    @property
    def providers(self) -> dict:
        """The toolsets a session's tool manager holds, by id: the doors every row below goes through."""
        return {"system": self.system, "workspaces": self.toolset, "trigger": self.trigger}

    async def snapshot(self) -> dict:
        """Everything a refused call must leave as it was: the workspace and session rows, and every live workspace's files."""
        rows = (await self.sp.get_storage(Workspace).list(OffsetPage(offset=0, length=100))).items
        sessions = (await self.sp.get_storage(WorkspaceSession).list(OffsetPage(offset=0, length=100))).items
        return {
            "workspaces": sorted(r.model_dump_json() for r in rows),
            "sessions": sorted(s.model_dump_json() for s in sessions),
            "files": {wid: dict(ws.files) for wid, ws in self.backend.live.items()},
        }


async def _ws_world() -> WsWorld:
    """Three workspaces on one provider (``ws-prime`` is the default), the real ``WorkspaceRegistry`` over them, a calling session in each, and the
    session ``ses-t`` in ``ws-2`` whose transcript the tap drains."""
    sp = _FakeStorageProvider()
    await sp.set_default_workspace_id("ws-prime")
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(id="local-1", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/nonexistent")),
    )
    for wid in ("ws-1", "ws-2", "ws-prime"):
        await sp.get_storage(Workspace).create(Workspace(
            id=wid, name=wid, template_id="tpl", provider_id="local-1", created_at=NOW, phase="running",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
        ))
    sessions = sp.get_storage(WorkspaceSession)
    for sid, wid in (("ses-a", "ws-1"), ("ses-o", "ws-2"), ("ses-p", "ws-prime"), ("ses-t", "ws-2")):
        await sessions.create(WorkspaceSession(
            id=sid, workspace_id=wid, binding=AgentSessionBinding(agent_id="planner"), status=SessionStatus.WAITING, created_at=NOW,
            initiated_by=USER,
        ))
    backend = _Backend()
    workspace_registry = WorkspaceRegistry(sp, factory=lambda row: backend)
    workspaces = build_workspaces_toolset(storage_provider=sp, workspace_registry=workspace_registry)
    registry = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(), cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda p: object(),
    )
    registry._workspaces_toolset_provider = workspaces  # noqa: SLF001 -- as the app lifespan wires it
    system = build_system_toolset(storage_provider=sp, provider_registry=registry, workspace_registry=workspace_registry)
    registry._system_toolset_provider = system  # noqa: SLF001
    trigger = build_trigger_toolset_provider(storage_provider=sp)
    registry._trigger_toolset_provider = trigger  # noqa: SLF001
    return WsWorld(sp=sp, backend=backend, toolset=workspaces, system=system, trigger=trigger, registry=registry, workspaces=workspace_registry)


def _args(tool: str, target: str) -> dict:
    return {k: (target if v == W else v) for k, v in WS_TOOLS.get(tool, {}).items()}


#: The calling session in each workspace (``_ws_world`` stores them).
CALLERS = {"ws-1": "ses-a", "ws-2": "ses-o", "ws-prime": "ses-p"}
MCP_USER = Principal(type="user", id="u-mcp", display="mcp", role="user", source="local")


async def _call(
    world: WsWorld, toolset: str, tool: str, arguments: dict, *, own: str | None, door: str, run_as: PrincipalRef = USER,
    actor: Principal = MCP_USER,
) -> tuple[str, dict]:
    """``toolset``'s ``tool`` from a session in ``own`` (its run ranked ``run_as``) through ``door``: the tool by its scoped name in the real
    tool manager, or ``system__call_tool``. With ``own`` None it is the MCP endpoint's own call (``invoke_one``, no context, ``actor`` on
    ``current_actor``)."""
    if own is None:
        token = current_actor.set(actor)
        try:
            result = await invoke_one(provider=world.providers[toolset], tool_name=tool, arguments=arguments, principal=None)
        finally:
            current_actor.reset(token)
        output, is_error = result.output, result.is_error
    else:
        manager = ToolExecutionManager(
            toolset_providers=world.providers,
            workspace_session=SimpleNamespace(session_id=CALLERS[own], workspace_id=own, agent_id="planner"),  # type: ignore[arg-type]
            initiated_by=run_as,
        )
        if door == "tool manager":
            call = ToolCallPart(id="call-1", name=f"{toolset}__{tool}", arguments=arguments)
        else:
            call = ToolCallPart(
                id="call-1", name="system__call_tool", arguments={"toolset_id": toolset, "tool_name": tool, "arguments": arguments},
            )
        part = await manager.execute(call)
        output, is_error = part.output, part.error
    body = json.loads(output)
    return (body["type"] if is_error else D), body


async def _ws_call(world: WsWorld, tool: str, target: str, *, own: str | None, door: str) -> tuple[str, dict]:
    """``tool`` on ``target``, from a session in ``own`` through ``door``, or with no calling session (``own`` None) as the MCP endpoint calls it."""
    return await _call(world, "workspaces", tool, _args(tool, target), own=own, door=door)


async def _ws_delivered(world: WsWorld, tool: str, body: dict) -> bool:
    """Whether the call did its work: read off the world for a write, off the answer for a read."""
    live = world.backend.live.get("ws-2")
    if tool == "delete_workspace":
        return live is None and await world.sp.get_storage(Workspace).get("ws-2") is None
    if tool == "write_workspace_file":
        return live.files["notes.txt"] == b"changed"
    if tool == "delete_workspace_file":
        return "notes.txt" not in live.files
    return {
        "get_workspace": lambda: body.get("id") == "ws-2",
        "workspace_tap": lambda: [e["session_id"] for e in body["events"]] == ["ses-t"],
        "get_workspace_log": lambda: [c["workspace_id"] for c in body["commits"]] == ["ws-2"],
        "list_workspace_files": lambda: [i["path"] for i in body["items"]] == ["notes.txt"],
        "get_workspace_file_info": lambda: body.get("path") == "notes.txt",
        "read_workspace_file": lambda: body.get("content") == "hello from ws-2",
    }[tool]()


_WS_ROWS = [(tool, who, door) for tool in WS_TOOLS for who in WS_CALLERS if who != "no session" for door in DOORS] + [
    (tool, "no session", "MCP") for tool in WS_TOOLS
]


@pytest.mark.parametrize(("tool", "who", "door"), _WS_ROWS)
async def test_every_tool_that_acts_on_a_workspace_obeys_the_rule_table(tool, who, door):
    """A session outside prime reaches only its own workspace; out of reach answers exactly as an unknown workspace and changes nothing."""
    world = await _ws_world()
    own, expected = WS_CALLERS[who]
    before = await world.snapshot()

    outcome, body = await _ws_call(world, tool, "ws-2", own=own, door=door)

    assert outcome == expected, (tool, who, door, body)
    if outcome == D:
        assert await _ws_delivered(world, tool, body), (tool, who, door, body)
        return
    assert await world.snapshot() == before, f"{tool} changed the workspace it refused ({who}, {door})"
    # The out-of-reach answer is exactly what an unknown workspace answers (asked with no calling session, past every reach rule).
    _, unknown = await _ws_call(world, tool, "ws-nope", own=None, door="MCP")
    assert json.dumps(body).replace("ws-2", "<ws>") == json.dumps(unknown).replace("ws-nope", "<ws>"), (body, unknown)


@pytest.mark.parametrize("door", DOORS)
async def test_delete_workspace_from_outside_prime_naming_prime_answers_as_an_unknown_workspace(door):
    """Prime is out of reach too: the session is told it does not exist (not that it is reserved), and nothing is torn down."""
    world = await _ws_world()
    before = await world.snapshot()

    outcome, body = await _ws_call(world, "delete_workspace", "ws-prime", own="ws-1", door=door)

    assert (outcome, body) == (N, {"type": N, "message": "Workspace 'ws-prime' does not exist"})
    assert await world.snapshot() == before


@pytest.mark.parametrize(("own", "door", "listed"), [
    pytest.param("ws-1", "tool manager", ["ws-1"], id="a session outside prime, by name"),
    pytest.param("ws-1", "system__call_tool", ["ws-1"], id="a session outside prime, through call_tool"),
    pytest.param("ws-prime", "system__call_tool", ["ws-1", "ws-2", "ws-prime"], id="a session in prime"),
    pytest.param(None, "MCP", ["ws-1", "ws-2", "ws-prime"], id="no calling session (MCP)"),
])
async def test_list_workspaces_lists_only_what_the_caller_reaches(own, door, listed):
    """The finder of workspaces follows the rule of ``find_sessions``: a session outside prime lists only its own workspace, so a workspace id it
    cannot reach is never shown to it (``get_workspace`` answers such an id as unknown)."""
    world = await _ws_world()

    outcome, body = await _ws_call(world, "list_workspaces", "", own=own, door=door)

    assert outcome == D, body
    assert sorted(item["id"] for item in body["items"]) == listed
    assert body["total"] == len(listed)


# ---- The follow-ups of #870 (tickets 01a16ba1-da9b and 01a16ba1-e2d1) --------------------------------------------------------------------
#
# The doors the review of #870 found still open from a session outside prime. Every row goes through the real tool manager (the tool by its scoped
# name, and ``system__call_tool``) or as the MCP endpoint calls it; the fire rows go through the worker's trigger handler
# (``primer.worker.engine_handlers.run_engine_trigger``), which runs ``fire_trigger`` and the real subscription dispatchers.


def _rows(doors) -> list[tuple]:
    return [(d, who, door) for d in doors for who in WS_CALLERS if who != "no session" for door in DOORS] + [(d, "no session", "MCP") for d in doors]


# -- the reply-binding tools (F2) -------------------------------------------------------------------------------------------------------------

REPLY_DOORS = {
    "set_reply_binding": {"workspace_id": W, "channel_id": "ch-1"},
    "clear_reply_binding": {"workspace_id": W},
}


async def _reply_world() -> WsWorld:
    world = await _ws_world()
    await world.sp.get_storage(Channel).create(Channel(
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM, external_id="777", config=TelegramChannelConfig(),
    ))
    store = world.sp.get_storage(Workspace)
    await store.update((await store.get("ws-2")).model_copy(update={"reply_binding": WorkspaceChannelLink(channel_id="ch-old")}))
    return world


def _reply_args(tool: str, target: str, **extra) -> dict:
    return {**{k: (target if v == W else v) for k, v in REPLY_DOORS[tool].items()}, **extra}


@pytest.mark.parametrize(("tool", "who", "door"), _rows(REPLY_DOORS))
async def test_the_reply_binding_tools_reach_only_what_the_caller_reaches(tool, who, door):
    """F2: ``set_reply_binding`` and ``clear_reply_binding`` name a workspace like the workspaces tools do and follow the same reach."""
    world = await _reply_world()
    own, expected = WS_CALLERS[who]
    before = await world.snapshot()

    outcome, body = await asyncio.wait_for(_call(world, "system", tool, _reply_args(tool, "ws-2"), own=own, door=door), 30)

    assert outcome == expected, (tool, who, door, body)
    binding = (await world.sp.get_storage(Workspace).get("ws-2")).reply_binding
    if outcome == D:
        assert (binding.channel_id if binding else None) == ("ch-1" if tool == "set_reply_binding" else None), binding
        return
    assert await world.snapshot() == before, f"{tool} changed the workspace it refused ({who}, {door})"
    _, unknown = await _call(world, "system", tool, _reply_args(tool, "ws-nope"), own=own, door=door)
    assert json.dumps(body).replace("ws-2", "<ws>") == json.dumps(unknown).replace("ws-nope", "<ws>"), (body, unknown)


@pytest.mark.parametrize("channel", ["ch-1", "no-such-channel"])
@pytest.mark.parametrize("door", DOORS)
async def test_set_reply_binding_out_of_reach_answers_as_an_unknown_workspace_whatever_the_channel(channel, door):
    """The reach is asked before the channel is looked up, so an existing workspace out of reach and an unknown one answer alike, a channel that
    does not exist included: the tool confirms no workspace id."""
    world = await _reply_world()
    before = await world.snapshot()
    answers = [
        (await _call(world, "system", "set_reply_binding", _reply_args("set_reply_binding", target, channel_id=channel), own="ws-1", door=door))
        for target in ("ws-2", "ws-prime", "ws-nope")
    ]
    assert answers == [(N, {"type": N, "message": f"Workspace {t!r} does not exist"}) for t in ("ws-2", "ws-prime", "ws-nope")]
    assert await world.snapshot() == before


# -- trigger subscriptions and channel bindings (F1) ------------------------------------------------------------------------------------------

TRIGGER, CHANNEL_TRIGGER = "tr-1", "tr-ch"
_WS_TARGET, _SES_TARGET = ("ws-2", "ws-nope"), ("ses-t", "ses-nobody")

#: door -> (toolset, tool, arguments for a target, (target, unknown id)). A workspace target is ws-2; a session target is ses-t, in ws-2.
SUB_DOORS: dict[str, tuple[str, str, Callable[[str], dict], tuple[str, str]]] = {
    "trigger::create_subscription, agent_fresh_session": (
        "trigger", "create_subscription",
        lambda t: {"trigger_id": TRIGGER, "config": {"kind": "agent_fresh_session", "workspace_id": t, "agent_id": "planner"}}, _WS_TARGET,
    ),
    "trigger::create_subscription, graph_fresh_session": (
        "trigger", "create_subscription",
        lambda t: {"trigger_id": TRIGGER, "config": {"kind": "graph_fresh_session", "workspace_id": t, "graph_id": "g-1"}}, _WS_TARGET,
    ),
    "trigger::create_subscription, session_append": (
        "trigger", "create_subscription", lambda t: {"trigger_id": TRIGGER, "config": {"kind": "session_append", "session_id": t}}, _SES_TARGET,
    ),
    "system::create_channel_binding, agent_fresh_session": (
        "system", "create_channel_binding",
        lambda t: {"trigger_id": CHANNEL_TRIGGER, "config": {"kind": "agent_fresh_session", "workspace_id": t, "agent_id": "planner"}}, _WS_TARGET,
    ),
    "system::create_channel_binding, session_append": (
        "system", "create_channel_binding",
        lambda t: {"trigger_id": CHANNEL_TRIGGER, "config": {"kind": "session_append", "session_id": t}}, _SES_TARGET,
    ),
}
#: Who calls -> the reach recorded on what it saves: the calling session's workspace outside prime, ``None`` (every workspace) otherwise.
RECORDED = {"own workspace": "ws-2", P: None, "no session": None}


async def _seed_triggers(world: WsWorld) -> None:
    triggers = world.sp.get_storage(Trigger)
    await triggers.create(Trigger(
        id=TRIGGER, slug="tr-one", name="t", created_at=NOW, owner=USER, config=DelayedTriggerConfig(fire_at=NOW + timedelta(days=1)),
    ))
    await triggers.create(Trigger(id=CHANNEL_TRIGGER, slug="tr-ch", name="c", created_at=NOW, owner=USER, config=ChannelTriggerConfig(provider_id="cp-1")))


async def _subscriptions(world: WsWorld) -> dict[str, Subscription]:
    return {s.id: s for s in (await world.sp.get_storage(Subscription).list(OffsetPage(offset=0, length=100))).items}


@pytest.mark.parametrize(("door_name", "who", "door"), _rows(SUB_DOORS))
async def test_a_subscription_or_channel_binding_names_only_what_the_caller_reaches(door_name, who, door):
    """F1: a session outside prime may not save a subscription that starts or feeds a session outside its reach. Out of reach answers exactly as an
    unknown workspace or session does (from the same caller) and stores nothing; within reach the row records the reach it was saved with."""
    world = await _seed_world_for_subscriptions()
    toolset, tool, arguments, (target, unknown) = SUB_DOORS[door_name]
    own, expected = WS_CALLERS[who]
    before = await _subscriptions(world)

    outcome, body = await asyncio.wait_for(_call(world, toolset, tool, arguments(target), own=own, door=door), 30)

    assert outcome == expected, (door_name, who, door, body)
    after = await _subscriptions(world)
    if outcome == D:
        (new,) = [row for sid, row in after.items() if sid not in before]
        assert new.reach == RECORDED[who], (door_name, who, new)
        return
    assert after == before, f"{door_name} stored a subscription it refused ({who}, {door})"
    _, unknown_body = await _call(world, toolset, tool, arguments(unknown), own=own, door=door)
    assert json.dumps(body).replace(target, "<id>") == json.dumps(unknown_body).replace(unknown, "<id>"), (body, unknown_body)
    assert await _subscriptions(world) == before


async def _seed_world_for_subscriptions() -> WsWorld:
    world = await _ws_world()
    await _seed_triggers(world)
    return world


@pytest.mark.parametrize(("who", "door"), [(who, door) for who in WS_CALLERS if who != "no session" for door in DOORS] + [("no session", "MCP")])
async def test_an_update_of_a_subscription_from_a_session_reaches_only_a_target_it_reaches(who, door):
    """F1: updating a subscription saves it as the caller (its owner, and now its reach). From a session outside prime an update of a subscription
    whose target is out of reach answers exactly as a subscription id that does not exist (``subscription_not_found``; review of #874, C1) and
    changes nothing; within reach the row records the caller's reach."""
    world = await _seed_world_for_subscriptions()
    store = world.sp.get_storage(Subscription)
    await store.create(Subscription(
        id="sb-p", trigger_id=TRIGGER, config=AgentFreshSubConfig(workspace_id="ws-2", agent_id="planner"), created_at=NOW, owner=ADMIN,
    ))
    own, expected = WS_CALLERS[who]
    expected = "subscription_not_found" if expected == N else expected
    before = await store.get("sb-p")

    def _update(sid: str) -> dict:
        return {"trigger_id": TRIGGER, "subscription_id": sid, "payload_template": "changed"}

    outcome, body = await asyncio.wait_for(_call(world, "trigger", "update_subscription", _update("sb-p"), own=own, door=door), 30)

    assert outcome == expected, (who, door, body)
    after = await store.get("sb-p")
    if outcome == D:
        assert (after.payload_template, after.reach) == ("changed", RECORDED[who]), after
        return
    assert after == before, f"a refused update changed the subscription ({who}, {door})"
    _, unknown = await _call(world, "trigger", "update_subscription", _update("sb-nope"), own=own, door=door)
    assert body == {"type": "subscription_not_found", "message": "sb-p"}
    assert json.dumps(body).replace("sb-p", "<id>") == json.dumps(unknown).replace("sb-nope", "<id>"), (body, unknown)


# -- the fire ---------------------------------------------------------------------------------------------------------------------------------


class _FireWorkspace(FakeWorkspace):
    """The peer world's workspace, which also takes the slot a fresh session is started in."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[str] = []

    async def start_session(self, binding, *, id: str, instructions=None, parent_session_id=None, name=None) -> None:  # noqa: A002
        self.started.append(id)


class _FireRegistry(FakeRegistry):
    def workspace(self, workspace_id: str) -> _FireWorkspace:
        return self.workspaces.setdefault(workspace_id, _FireWorkspace())  # type: ignore[return-value]


def _runnable_graph() -> Graph:
    return Graph(
        id="g-1", description="begin -> end", nodes=[_BeginNode(id="begin"), _EndNode(id="end")],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )


async def _fire_world() -> World:
    """Three workspaces (``ws-prime`` the default), the subscription owner's account, an agent and a graph to start, ``ses-t`` (in ws-2) to steer
    and ``ses-pk`` (in ws-2) parked on a trigger subscription."""
    world = World(sp=_FakeStorageProvider(), registry=_FireRegistry(), scheduler=FakeScheduler(), engine=FakeEngine(), bus=FakeBus())
    await world.sp.set_default_workspace_id("ws-prime")
    for wid in ("ws-1", "ws-2", "ws-prime"):
        await world.sp.get_storage(Workspace).create(Workspace(
            id=wid, name=wid, template_id="tpl", provider_id="local-1", created_at=NOW, phase="running",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
        ))
    await world.sp.get_storage(User).create(User(id="u-user", username="user", created_at=NOW, role="user"))
    await world.sp.get_storage(Agent).create(Agent(id="planner", description="x", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]))
    await world.sp.get_storage(Graph).create(_runnable_graph())
    await world.sp.get_storage(Trigger).create(Trigger(
        id="tr-fire", slug="tr-fire", name="f", created_at=NOW, owner=USER, config=DelayedTriggerConfig(fire_at=NOW),
    ))
    await seed_session(world, sid="ses-t", workspace_id="ws-2", turn_no=1, created_at=NOW, initiated_by=USER)
    key = "trigger:ses-pk:tr-fire"
    await seed_session(
        world, sid="ses-pk", workspace_id="ws-2", turn_no=1, created_at=NOW, initiated_by=USER, turn_status="idle", parked_status="parked",
        parked_event_key=key,
        parked_state={
            "tool_call_id": "tc-1",
            "yielded": {"tool_name": "subscribe_to_trigger", "event_key": key, "resume_metadata": {"tool_call_id": "tc-1", "subscription_id": "sb-fire"}},
        },
    )
    return world


FIRE_KINDS: dict[str, tuple[object, str | None]] = {
    "agent_fresh_session": (AgentFreshSubConfig(workspace_id="ws-2", agent_id="planner"), None),
    "graph_fresh_session": (GraphFreshSubConfig(workspace_id="ws-2", graph_id="g-1"), '{"q": 1}'),
    "session_append": (SessionAppendSubConfig(session_id="ses-t"), None),
    "parked_session": (ParkedSessionSubConfig(session_id="ses-pk", tool_call_id="tc-1", parked_at=NOW), None),
}


async def _what_the_fire_did(world: World, kind: str, before: dict) -> bool:
    """Whether the fire started or fed a session in ws-2."""
    if kind.endswith("fresh_session"):
        started = world.registry.workspace("ws-2").started
        rows = (await world.sessions.list(OffsetPage(offset=0, length=100))).items
        return bool(started) or len(rows) != len(before)
    if kind == "session_append":
        return (await world.sessions.get("ses-t")).model_dump(mode="json") != before["ses-t"]
    return bool(world.bus.published)


@pytest.mark.parametrize("kind", list(FIRE_KINDS))
@pytest.mark.parametrize(("reach", "fed"), [
    pytest.param("ws-1", False, id="recorded reach ws-1, target in ws-2"),
    pytest.param("ws-2", True, id="recorded reach ws-2, target in ws-2"),
    pytest.param(None, True, id="no recorded reach (prime, a person, a row saved before reach was recorded)"),
])
async def test_a_fire_never_starts_or_feeds_a_session_outside_the_reach_recorded_on_its_subscription(kind, reach, fed):
    """F1 at fire time: the worker's trigger handler fires the trigger, and a subscription whose target is outside the reach recorded on it is refused
    and logged (``target_out_of_reach``, a failed delivery on the subscription) and never starts or feeds a session. A row with no recorded reach
    fires as before."""
    world = await _fire_world()
    config, payload = FIRE_KINDS[kind]
    await world.sp.get_storage(Subscription).create(Subscription.model_validate({
        "id": "sb-fire", "trigger_id": "tr-fire", "config": config.model_dump(mode="json"), "payload_template": payload, "parallelism": "queue",
        "created_at": NOW.isoformat(), "owner": USER.model_dump(mode="json"), "reach": reach,
    }))
    before = {s.id: s.model_dump(mode="json") for s in (await world.sessions.list(OffsetPage(offset=0, length=100))).items}
    released: list = []

    async def _release(lease, outcome) -> None:
        released.append(outcome)

    pool = SimpleNamespace(
        _storage=world.sp, _engine=world.engine, _scheduler=world.scheduler, _workspace_registry=world.registry, _event_bus=world.bus,
        _release_lease=_release,
    )
    await asyncio.wait_for(run_engine_trigger(pool, SimpleNamespace(entity_id="tr-fire")), 30)  # type: ignore[arg-type]

    assert await _what_the_fire_did(world, kind, before) is fed, (kind, reach)
    sub = await world.sp.get_storage(Subscription).get("sb-fire")
    if fed:
        assert sub is None or sub.last_fire_error is None, sub.last_fire_error
        return
    assert json.loads(sub.last_fire_error)["code"] == "target_out_of_reach", sub.last_fire_error
    assert [o.success for o in released] == [False], "a refused fire is a failed delivery, not a clean run"


# -- delete_workspace and the rank rule (F3) --------------------------------------------------------------------------------------------------


async def _admin_session_in_ws2(world: WsWorld, *, status: SessionStatus = SessionStatus.WAITING) -> None:
    await world.sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="ses-adm", workspace_id="ws-2", binding=AgentSessionBinding(agent_id="planner"), status=status, created_at=NOW, initiated_by=ADMIN,
    ))


_IN_SESSION_RANK = REFUSE_RANK.format(session_id="ses-adm", target_rank="admin", sender_rank="user")


@pytest.mark.parametrize(("who", "door", "message"), [
    pytest.param("own workspace", "tool manager", _IN_SESSION_RANK, id="a user-rank session in ws-2, by name"),
    pytest.param("own workspace", "system__call_tool", _IN_SESSION_RANK, id="a user-rank session in ws-2, through call_tool"),
    pytest.param(P, "system__call_tool", _IN_SESSION_RANK, id="a user-rank session in prime"),
    pytest.param("no session", "MCP", OUTRANKED_MESSAGE, id="a user over MCP"),
])
async def test_delete_workspace_is_refused_while_a_session_of_a_higher_rank_is_open_in_it(who, door, message):
    """F3: deleting a workspace ends every open session in it, so it follows the cancel tool's rank rule for each of them: refused (``forbidden``, in
    the words a cancel of that session is refused in) while one ranks above the caller, and nothing is torn down."""
    world = await _ws_world()
    await _admin_session_in_ws2(world)
    before = await world.snapshot()

    outcome, body = await asyncio.wait_for(_ws_call(world, "delete_workspace", "ws-2", own=WS_CALLERS[who][0], door=door), 30)

    assert (outcome, body) == (F, {"type": F, "message": message})
    assert await world.snapshot() == before


@pytest.mark.parametrize(("case", "own", "door"), [
    pytest.param("an admin run", "ws-2", "system__call_tool", id="an admin-rank session in ws-2"),
    pytest.param("an admin run", None, "MCP", id="an admin over MCP"),
    pytest.param("the session has ended", "ws-2", "tool manager", id="a user-rank session, the admin's session ended"),
])
async def test_delete_workspace_goes_on_when_every_open_session_in_it_is_at_or_below_the_caller(case, own, door):
    world = await _ws_world()
    await _admin_session_in_ws2(world, status=SessionStatus.ENDED if case == "the session has ended" else SessionStatus.WAITING)
    admin = Principal(type="user", id="u-root", display="root", role="admin", source="local")

    outcome, body = await asyncio.wait_for(_call(
        world, "workspaces", "delete_workspace", {"id": "ws-2"}, own=own, door=door, run_as=ADMIN if case == "an admin run" else USER, actor=admin,
    ), 30)

    assert outcome == D, body
    assert await world.sp.get_storage(Workspace).get("ws-2") is None


async def test_the_rest_delete_follows_the_rank_rule_of_the_rest_cancel(app, client, fake_storage_provider):
    """F3 over REST: the cancel route refuses a person ranked below the session's creator (403 ``forbidden_rank``), so the delete of the workspace
    the session is open in does too; an admin deletes it."""
    world = await _ws_world()
    backend = world.backend
    app.state.workspace_registry = WorkspaceRegistry(fake_storage_provider, factory=lambda row: backend)
    await fake_storage_provider.set_default_workspace_id("ws-prime")
    for wid in ("ws-2", "ws-prime"):
        await fake_storage_provider.get_storage(Workspace).create(await world.sp.get_storage(Workspace).get(wid))
    await fake_storage_provider.get_storage(WorkspaceProvider).create(await world.sp.get_storage(WorkspaceProvider).get("local-1"))
    await fake_storage_provider.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="ses-adm", workspace_id="ws-2", binding=AgentSessionBinding(agent_id="planner"), status=SessionStatus.WAITING, created_at=NOW,
        initiated_by=ADMIN,
    ))
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
        assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200

        refused = await asyncio.wait_for(carol.delete("/v1/workspaces/ws-2"), 30)

    assert refused.status_code == 403, refused.text
    assert (refused.json()["detail"], refused.json()["extensions"]["error"]) == (OUTRANKED_MESSAGE, "forbidden_rank")
    assert await fake_storage_provider.get_storage(Workspace).get("ws-2") is not None
    assert (await fake_storage_provider.get_storage(WorkspaceSession).get("ses-adm")).status == SessionStatus.WAITING
    assert "ws-2" in backend.live

    deleted = await asyncio.wait_for(client.delete("/v1/workspaces/ws-2"), 30)

    assert deleted.status_code == 204, deleted.text
    assert await fake_storage_provider.get_storage(Workspace).get("ws-2") is None


# -- create_workspace from a session outside prime (F4) ---------------------------------------------------------------------------------------


async def _template_world() -> WsWorld:
    world = await _ws_world()
    await world.sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(id="tpl", description="t", provider_id="local-1"))
    return world


@pytest.mark.parametrize("arguments", [
    pytest.param({"id": "ws-new", "template_id": "tpl"}, id="a new id"),
    pytest.param({"id": "ws-2", "template_id": "tpl"}, id="an id that exists"),
    pytest.param({"template_id": "no-such-template"}, id="an unknown template"),
])
@pytest.mark.parametrize(("own", "door"), [("ws-1", "tool manager"), ("ws-1", "system__call_tool"), ("ws-2", "system__call_tool")])
async def test_create_workspace_is_refused_from_a_session_outside_prime(arguments, own, door):
    """F4: a session outside prime creates no workspace (it could not reach it anyway). The refusal comes before the template and id lookups, so it
    confirms neither, and it says where workspaces are made."""
    world = await _template_world()
    before = await world.snapshot()

    outcome, body = await asyncio.wait_for(_call(world, "workspaces", "create_workspace", arguments, own=own, door=door), 30)

    assert outcome == F, body
    assert await world.snapshot() == before
    from primer.toolset.workspaces import REFUSE_CREATE_WORKSPACE

    assert body == {"type": F, "message": REFUSE_CREATE_WORKSPACE}
    assert "prime" in REFUSE_CREATE_WORKSPACE and "console" in REFUSE_CREATE_WORKSPACE


@pytest.mark.parametrize(("own", "door"), [("ws-prime", "tool manager"), ("ws-prime", "system__call_tool"), (None, "MCP")])
async def test_create_workspace_from_prime_or_with_no_session_creates_it(own, door):
    world = await _template_world()

    outcome, body = await asyncio.wait_for(_call(world, "workspaces", "create_workspace", {"id": "ws-new", "template_id": "tpl"}, own=own, door=door), 30)

    assert outcome == D, body
    assert (await world.sp.get_storage(Workspace).get("ws-new")) is not None and "ws-new" in world.backend.live


# -- a graph session with no holder slot (F6) -------------------------------------------------------------------------------------------------


class _HolderlessHost:
    """The live workspace of a graph session created before holder slots existed: a state repo, no session slot, no agents.md."""

    def __init__(self, workspace_id: str, state_repo: LocalStateRepo) -> None:
        self.id = workspace_id
        self.state_repo = state_repo

    async def get_session(self, session_id: str) -> None:
        return None

    async def file_info(self, path: str) -> FileEntry:
        raise NotFoundError(f"{path!r} not found")

    async def read_file(self, path: str) -> bytes:
        raise NotFoundError(f"{path!r} not found")


@pytest.mark.parametrize(("own", "expected"), [pytest.param("ws-1", N, id="a graph session outside prime"), pytest.param("ws-prime", D, id="in prime")])
@pytest.mark.parametrize("door", DOORS)
async def test_the_tools_of_a_graph_session_with_no_holder_slot_reach_only_what_its_workspace_reaches(own, expected, door, tmp_path):
    """F6: the worker builds a holder-less graph session's per-node tool manager with no workspace session (``build_graph_executor``). Its tools
    used to get no context at all, which the workspaces tools read as a caller with no session: every workspace. The manager now names the row's
    session and workspace on every context it builds, so the node's tools reach what the session's workspace reaches."""
    world = await _ws_world()
    graph = _runnable_graph()
    row = WorkspaceSession(
        id="ses-g", workspace_id=own, binding=GraphSessionBinding(graph_id=graph.id, graph_snapshot=graph), status=SessionStatus.RUNNING,
        created_at=NOW, initiated_by=USER,
    )
    await world.sp.get_storage(WorkspaceSession).create(row)
    await world.sp.get_storage(User).create(User(id="u-user", username="user", created_at=NOW, role="user"))
    repo = LocalStateRepo(tmp_path / "state", workspace_id=own)
    await repo.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=world.sp, workspace_registry=world.workspaces,  # type: ignore[arg-type]
        provider_registry=world.registry, engine=InMemoryClaimEngine(adapters={}),  # type: ignore[arg-type]
    )
    driver = await pool._build_executor(row, _HolderlessHost(own, repo))  # noqa: SLF001 -- the worker's own build seam
    agent = Agent(
        id="planner", description="x", model=AgentModel(profile_id="p--m"), tools=["workspaces__delete_workspace", "system__call_tool"],
        system_prompt=[],
    )
    manager = await driver._executor._tool_manager_resolver(agent)  # noqa: SLF001 -- what every agent node of the run is given

    ctx = manager.context_for("call-1")
    assert ctx is not None and (ctx.session_id, ctx.workspace_id, ctx.initiated_by) == ("ses-g", own, USER)
    before = await world.snapshot()
    if door == "tool manager":
        call = ToolCallPart(id="call-1", name="workspaces__delete_workspace", arguments={"id": "ws-2"})
    else:
        call = ToolCallPart(
            id="call-1", name="system__call_tool", arguments={"toolset_id": "workspaces", "tool_name": "delete_workspace", "arguments": {"id": "ws-2"}},
        )
    part = await asyncio.wait_for(manager.execute(call), 30)

    body = json.loads(part.output)
    assert (body["type"] if part.error else D) == expected, body
    if expected == N:
        assert body == {"type": N, "message": "Workspace 'ws-2' does not exist"}
        assert await world.snapshot() == before
    else:
        assert await world.sp.get_storage(Workspace).get("ws-2") is None


# -- the trigger-level tools (review of #874, B1) ---------------------------------------------------------------------------------------------
#
# A trigger is not in a workspace, but its subscriptions are: updating (re-enabling, re-timing), firing or deleting it acts on every one of them. From
# a session outside prime, ``trigger::update``, ``trigger::fire_now`` and ``trigger::delete`` are refused, exactly as an unknown trigger, when any
# subscription of the trigger targets a workspace or a session outside the caller's reach.

TRIGGER_DOORS: dict[str, dict] = {
    "update": {"id": "<trigger>", "name": "renamed"},
    "fire_now": {"id": "<trigger>"},
    "delete": {"id": "<trigger>"},
}


async def _trigger_world(target_workspace: str = "ws-2") -> WsWorld:
    """The rule-table world, a trigger ``tr-x`` (enabled, delayed) and one subscription of it, saved by a person (no recorded reach), starting a
    session in ``target_workspace``."""
    world = await _ws_world()
    await world.sp.get_storage(Trigger).create(Trigger(
        id="tr-x", slug="tr-x", name="t", created_at=NOW, owner=USER, config=DelayedTriggerConfig(fire_at=NOW + timedelta(days=1)),
    ))
    await world.sp.get_storage(Subscription).create(Subscription(
        id="sb-x", trigger_id="tr-x", config=AgentFreshSubConfig(workspace_id=target_workspace, agent_id="planner"), created_at=NOW, owner=USER,
    ))
    return world


def _trigger_args(tool: str, trigger_id: str) -> dict:
    return {k: (trigger_id if v == "<trigger>" else v) for k, v in TRIGGER_DOORS[tool].items()}


async def _trigger_acted(world: WsWorld, tool: str) -> bool:
    row = await world.sp.get_storage(Trigger).get("tr-x")
    if tool == "delete":
        return row is None
    if tool == "update":
        return row.name == "renamed"
    return row.last_fired_id is not None


async def _trigger_rows(world: WsWorld) -> tuple:
    return (
        (await world.sp.get_storage(Trigger).get("tr-x")).model_dump(mode="json"),
        sorted(s.model_dump_json() for s in (await _subscriptions(world)).values()),
    )


@pytest.mark.parametrize(("tool", "who", "door"), _rows(TRIGGER_DOORS))
async def test_the_trigger_level_tools_act_only_on_a_trigger_whose_subscriptions_the_caller_reaches(tool, who, door):
    """B1: the subscription targets ws-2. From ws-1 the trigger answers exactly as an unknown one (``trigger_not_found``) and nothing changes; from
    ws-2 (its own workspace), from prime and with no session the tool acts."""
    world = await _trigger_world()
    own, expected = WS_CALLERS[who]
    expected = "trigger_not_found" if expected == N else expected
    before = await _trigger_rows(world)

    outcome, body = await asyncio.wait_for(_call(world, "trigger", tool, _trigger_args(tool, "tr-x"), own=own, door=door), 30)

    assert outcome == expected, (tool, who, door, body)
    if outcome == D:
        assert await _trigger_acted(world, tool), (tool, who, door)
        return
    assert await _trigger_rows(world) == before, f"{tool} changed the trigger it refused ({who}, {door})"
    _, unknown = await _call(world, "trigger", tool, _trigger_args(tool, "tr-nope"), own=own, door=door)
    assert json.dumps(body).replace("tr-x", "<id>") == json.dumps(unknown).replace("tr-nope", "<id>"), (body, unknown)


@pytest.mark.parametrize("tool", list(TRIGGER_DOORS))
async def test_a_trigger_whose_subscriptions_are_all_in_reach_is_acted_on_from_outside_prime(tool):
    world = await _trigger_world(target_workspace="ws-1")

    outcome, body = await asyncio.wait_for(_call(world, "trigger", tool, _trigger_args(tool, "tr-x"), own="ws-1", door="tool manager"), 30)

    assert outcome == D, body
    assert await _trigger_acted(world, tool)


async def test_fire_now_from_outside_prime_does_not_wake_a_session_parked_in_another_workspace():
    """Probe A of the #874 review: ``ses-pk`` (ws-2) subscribed itself (reach ws-2), and a session in ws-1 fired the trigger: the park was woken."""
    world = await _fire_world()
    await world.sp.get_storage(Subscription).create(Subscription(
        id="sb-fire", trigger_id="tr-fire", config=ParkedSessionSubConfig(session_id="ses-pk", tool_call_id="tc-1", parked_at=NOW),
        created_at=NOW, owner=USER, reach="ws-2",
    ))
    trig = build_trigger_toolset_provider(storage_provider=world.sp, event_bus=world.bus)
    manager = ToolExecutionManager(
        toolset_providers={"trigger": trig},
        workspace_session=SimpleNamespace(session_id="ses-a", workspace_id="ws-1", agent_id="planner"),  # type: ignore[arg-type]
        initiated_by=USER,
    )

    part = await asyncio.wait_for(manager.execute(ToolCallPart(id="call-1", name="trigger__fire_now", arguments={"id": "tr-fire"})), 30)

    assert (part.error, json.loads(part.output)) == (True, {"type": "trigger_not_found", "message": "tr-fire"})
    assert world.bus.published == [], "a session in ws-1 woke a session parked in ws-2"


@pytest.mark.parametrize("subscription", [
    pytest.param(AgentFreshSubConfig(workspace_id="ws-prime", agent_id="planner"), id="a fresh session in prime"),
    pytest.param(SessionAppendSubConfig(session_id="ses-t"), id="a steer of a session in ws-2"),
])
async def test_update_from_outside_prime_cannot_re_enable_a_trigger_that_starts_or_feeds_a_session_out_of_reach(subscription):
    """Probe B of the #874 review: a person's disabled trigger whose subscription starts a session in prime. A session in ws-1 re-enabled it and set
    it due, and the worker's next fire started a session in prime. Refused now, so the fire finds the trigger disabled."""
    world = await _fire_world()
    await world.sp.get_storage(Trigger).create(Trigger(
        id="tr-off", slug="tr-off", name="nightly", created_at=NOW, owner=ADMIN, enabled=False,
        config=DelayedTriggerConfig(fire_at=NOW + timedelta(days=30)),
    ))
    await world.sp.get_storage(Subscription).create(Subscription.model_validate({
        "id": "sb-off", "trigger_id": "tr-off", "config": subscription.model_dump(mode="json"), "created_at": NOW.isoformat(),
        "owner": USER.model_dump(mode="json"), "parallelism": "queue",
    }))
    before = (await world.sessions.get("ses-t")).model_dump(mode="json")
    trig = build_trigger_toolset_provider(storage_provider=world.sp, event_bus=world.bus)
    manager = ToolExecutionManager(
        toolset_providers={"trigger": trig},
        workspace_session=SimpleNamespace(session_id="ses-a", workspace_id="ws-1", agent_id="planner"),  # type: ignore[arg-type]
        initiated_by=USER,
    )

    part = await asyncio.wait_for(manager.execute(ToolCallPart(
        id="call-1", name="trigger__update", arguments={"id": "tr-off", "enabled": True, "config": {"kind": "delayed", "fire_at": NOW.isoformat()}},
    )), 30)

    assert (part.error, json.loads(part.output)) == (True, {"type": "trigger_not_found", "message": "tr-off"})
    pool = SimpleNamespace(
        _storage=world.sp, _engine=world.engine, _scheduler=world.scheduler, _workspace_registry=world.registry, _event_bus=world.bus,
        _release_lease=lambda lease, outcome: asyncio.sleep(0),
    )
    await asyncio.wait_for(run_engine_trigger(pool, SimpleNamespace(entity_id="tr-off")), 30)  # type: ignore[arg-type]
    assert world.registry.workspace("ws-prime").started == [] and world.registry.workspace("ws-2").started == []
    assert (await world.sessions.get("ses-t")).model_dump(mode="json") == before


# -- the rank scan of a delete is fenced against a session started meanwhile (review of #874, D1) ---------------------------------------------


async def test_a_session_started_while_a_delete_runs_its_rank_scan_is_refused(monkeypatch):
    """Probe D of the #874 review: the rank scan ran before the destroy, so an admin's session started between the two was ended by the delete
    without a rank check. The scan now runs inside ``destroy`` after it marks the workspace as being destroyed, and a session start refuses such a
    workspace: an admin's start that lands after the scan is refused (as a workspace that does not exist), so the delete ends no session it did not
    rank."""
    import primer.toolset.workspaces as wsmod
    from primer.workspace.session_factory import SessionFactoryDeps, start_workspace_session

    world = await _ws_world()
    await world.sp.get_storage(Agent).create(Agent(id="planner", description="x", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]))
    real_scan = wsmod.open_sessions_on
    admin_start: dict = {}

    async def _scan_then_an_admin_starts_a_session(sp, workspace_id):
        async for row in real_scan(sp, workspace_id):
            yield row
        try:
            admin_start["session"] = await start_workspace_session(
                workspace_id=workspace_id, binding=AgentSessionBinding(agent_id="planner"), initial_instructions="hi", graph_input=None,
                auto_start=False, metadata=None, parent_session_id=None, initiated_by=ADMIN,
                deps=SessionFactoryDeps(storage_provider=sp, claim_engine=FakeEngine(), scheduler=FakeScheduler(), workspace_registry=world.workspaces),
            )
        except NotFoundError as exc:
            admin_start["refused"] = exc.message

    monkeypatch.setattr(wsmod, "open_sessions_on", _scan_then_an_admin_starts_a_session)

    outcome, body = await asyncio.wait_for(_ws_call(world, "delete_workspace", "ws-2", own="ws-2", door="tool manager"), 30)

    assert admin_start == {"refused": "Workspace 'ws-2' does not exist"}, admin_start
    assert outcome == D and await world.sp.get_storage(Workspace).get("ws-2") is None, body
    sessions = (await world.sp.get_storage(WorkspaceSession).list(OffsetPage(offset=0, length=100))).items
    assert [s.id for s in sessions if s.initiated_by == ADMIN] == [], "the delete ended an admin's session it never ranked"
