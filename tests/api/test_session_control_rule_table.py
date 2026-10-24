"""The rule table's rows for the paths that act on a session without putting input into it (review of #853, the lead's EXTEND ruling).

A session outside the default workspace reaches only its own workspace, for every workspaces tool that names a session or a workspace: the cancel,
get, read-messages, pause, resume, interrupt and create tools (an agent that could create a session in prime would reach everything from there). An
out-of-reach target answers exactly as an unknown id does. Cancel and resume also follow the rank rule (``primer.authz.may_drive``) from a session,
over MCP and on their REST routes: they stop or start a run at the creator's rank. The drive rows are pinned by
``tests/api/test_session_drive_rule_table.py``; these are the rest of ``docs/dev/subsystems/sessions.md`` section 9's table.

The tool rows go through :func:`primer.agent.inner_dispatch.dispatch_inner_tool`, as ``system__call_tool`` reaches a tool (with the request's actor
on ``current_actor`` and no context for the MCP rows); the REST rows go over HTTP through the auth middleware as a user-role account.

The last section holds the rows of the tools that name a WORKSPACE rather than a session (ticket 01a16ad1): delete, get and list a workspace, its
tap, its log and its files. They follow the same reach rule and are driven through the real tool manager (by name and through ``system__call_tool``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.agent.tool_manager import ToolExecutionManager, invoke_one
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.mcp.server import current_actor
from primer.model.agent import Agent, AgentModel
from primer.model.chat import ToolCallPart
from primer.model.except_ import NotFoundError
from primer.model.principal import Principal, PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.workspace import (
    CommitInfo,
    FileEntry,
    LocalWorkspaceConfig,
    Workspace,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
)
from primer.model.workspace_session import AgentSessionBinding, SessionInfo, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.toolset.system import build_system_toolset
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

    async def destroy(self, workspace_id: str) -> None:
        if self.live.pop(workspace_id, None) is None:
            raise NotFoundError(f"workspace {workspace_id!r} not found")


@dataclass
class WsWorld:
    sp: _FakeStorageProvider
    backend: _Backend
    toolset: object
    system: object

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
    workspaces = build_workspaces_toolset(storage_provider=sp, workspace_registry=WorkspaceRegistry(sp, factory=lambda row: backend))
    registry = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(), cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda p: object(),
    )
    registry._workspaces_toolset_provider = workspaces  # noqa: SLF001 -- as the app lifespan wires it
    system = build_system_toolset(storage_provider=sp, provider_registry=registry)
    registry._system_toolset_provider = system  # noqa: SLF001
    return WsWorld(sp=sp, backend=backend, toolset=workspaces, system=system)


def _args(tool: str, target: str) -> dict:
    return {k: (target if v == W else v) for k, v in WS_TOOLS.get(tool, {}).items()}


async def _ws_call(world: WsWorld, tool: str, target: str, *, own: str | None, door: str) -> tuple[str, dict]:
    """``tool`` on ``target``, from a session in ``own`` through ``door``, or with no calling session (``own`` None) as the MCP endpoint calls it."""
    arguments = _args(tool, target)
    if own is None:
        token = current_actor.set(Principal(type="user", id="u-mcp", display="mcp", role="user", source="local"))
        try:
            result = await invoke_one(provider=world.toolset, tool_name=tool, arguments=arguments, principal=None)
        finally:
            current_actor.reset(token)
        output, is_error = result.output, result.is_error
    else:
        caller = {"ws-1": "ses-a", "ws-2": "ses-o", "ws-prime": "ses-p"}[own]
        manager = ToolExecutionManager(
            toolset_providers={"system": world.system, "workspaces": world.toolset},
            workspace_session=SimpleNamespace(session_id=caller, workspace_id=own, agent_id="planner"),  # type: ignore[arg-type]
            initiated_by=USER,
        )
        if door == "tool manager":
            call = ToolCallPart(id="call-1", name=f"workspaces__{tool}", arguments=arguments)
        else:
            call = ToolCallPart(
                id="call-1", name="system__call_tool", arguments={"toolset_id": "workspaces", "tool_name": tool, "arguments": arguments},
            )
        part = await manager.execute(call)
        output, is_error = part.output, part.error
    body = json.loads(output)
    return (body["type"] if is_error else D), body


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
