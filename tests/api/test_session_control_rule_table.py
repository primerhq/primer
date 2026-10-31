"""The rule table's rows for the paths that act on a session without putting input into it (review of #853, the lead's EXTEND ruling).

The TOOL rows (access-control P2 T6, where the reach and rank rules lived for the tools): the run's person acts, and must hold the level the tool
needs on the workspace it names (full for the control tools, read for the reads, owner for the delete): a workspace the person holds no level on
answers exactly as one that does not exist (``not-found``), a level below what the tool needs answers ``forbidden``, and the session's recorded
creator is no longer consulted. A call with no calling session (the MCP endpoint) is checked as the token's user; a token with no user behind it
names no person and answers not-found. The REST rows of cancel, resume, pause and interrupt (Stop), the DELETE of a session, the binding switch and the canvas routes follow the
workspace access the session inherits instead (P2 Task 3): the request's actor at or above full on the target's workspace acts, a stranger answers
the session's missing-row 404 and a holder of a lower level the 403 that names the level. The drive rows are pinned by ``tests/api/test_session_drive_rule_table.py``; these are the rest of
``docs/dev/subsystems/sessions.md`` section 9's table.

The tool rows go through :func:`primer.agent.inner_dispatch.dispatch_inner_tool`, as ``system__call_tool`` reaches a tool (with the request's actor
on ``current_actor`` and no context for the MCP rows); the REST rows go over HTTP through the auth middleware as a user-role account.

The next section holds the rows of the tools that name a WORKSPACE rather than a session (ticket 01a16ad1): delete, get and list a workspace, its
tap, its log and its files. They follow the same reach rule and are driven through the real tool manager (by name and through ``system__call_tool``).

The last section holds the follow-ups of #870 (tickets 01a16ba1-da9b and 01a16ba1-e2d1): a trigger subscription or a channel binding (created,
updated, and fired by the worker's trigger handler), the reply-binding tools, ``delete_workspace``'s rank rule, ``create_workspace`` from a session
outside prime, and the tools of a graph session that has no holder slot. Then who may fire a trigger now (ticket 01a16e65-12d1): the REST route and
the tool follow the rank rule against the trigger's owner, after the reach rule. Then the subscription reads and the single-subscription deletes
(ticket 01a16c22): a session outside prime lists, gets and deletes only the subscriptions whose target it reaches.
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
from pydantic import SecretStr

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.agent.tool_manager import ToolExecutionManager, invoke_one
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.authz import OUTRANKED_MESSAGE
from primer.claim.in_memory import InMemoryClaimEngine
from primer.mcp.server import current_actor
from primer.model.access import Access
from primer.model.agent import Agent, AgentModel
from primer.model.api_token import ApiToken
from primer.model.canvas import SessionCanvas
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
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    PendingSessionMessage,
    SessionInfo,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolContext
from primer.toolset.system import build_system_toolset
from primer.toolset.trigger import build_trigger_toolset_provider
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from primer.trigger.service import _LIST_PAGE_SIZE
from primer.worker.engine_handlers import run_engine_trigger
from primer.worker.pool import WorkerPool
from primer.workspace.local.state import LocalStateRepo
from tests.api.test_session_steer_restart_rank import WHO as PERSONS
from tests.api.test_session_steer_restart_rank import _Spy, _access_for, _token_client, _user_id
from tests.session._peer_fakes import FakeBus, FakeEngine, FakeRegistry, FakeScheduler, FakeSlot, FakeWorkspace, World, seed_session
from tests.conftest import _FakeStorageProvider

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
USER = PrincipalRef(type="user", id="u-user", display="user", role="user", source="local", user_id="u-user")
ADMIN = PrincipalRef(type="user", id="u-root", display="root", role="admin", source="local")
#: The second person: the person the ``own workspace`` and ``from prime`` callers act as (access-control P2 T6): the visibility is keyed on the
#: person's level, not on the workspace the calling session sits in.
ADA_REF = PrincipalRef(type="user", id="u-ada", display="ada", role="user", source="local", user_id="u-ada")
#: A full grantee, for the delete's owner-level refusal.
BOB = PrincipalRef(type="user", id="u-bob", display="bob", role="user", source="local", user_id="u-bob")
D, F, N = "delivered", "forbidden", "not-found"
P = "from prime"

#: path -> (target status, what each scenario answers). ``outranked``: the target was started by an admin (the tools no longer rank the creator;
#: the REST rows still do); ``same rank``: by a user; ``out of reach``: the target is in another workspace than the calling session's, one the
#: caller's person holds no level on; ``from prime``: the calling session is in the default workspace, the person holds full on the target. A
#: tool with no calling session acts as the token's user, which here has no user behind it: not-found everywhere. For the REST rows the scenarios read as
#: the target's workspace access (P2 Task 3): ``outranked`` a workspace the caller holds no level on (the session's missing-row 404), ``same rank`` one
#: the caller owns, ``out of reach`` one the caller holds read on only (the 403 that names the level), ``from prime`` one the caller owns.
TABLE: dict[str, tuple[SessionStatus, dict[str, str]]] = {
    "cancel tool, in a session":        (SessionStatus.WAITING, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
    "cancel tool, no session":          (SessionStatus.WAITING, {"outranked": N, "same rank": N, "out of reach": N, P: N}),
    "REST cancel":                      (SessionStatus.WAITING, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    "resume tool, in a session":        (SessionStatus.PAUSED, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
    "resume tool, no session":          (SessionStatus.PAUSED, {"outranked": N, "same rank": N, "out of reach": N, P: N}),
    "REST resume":                      (SessionStatus.PAUSED, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    "pause tool, in a session":         (SessionStatus.WAITING, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
    "pause tool, no session":           (SessionStatus.WAITING, {"outranked": N, "same rank": N, "out of reach": N, P: N}),
    "REST pause":                       (SessionStatus.WAITING, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    # a RUNNING target (not parked), so a delivered Stop is recorded (``interrupt_requested``) and a refused one is seen to record nothing
    "interrupt tool, in a session":     (SessionStatus.RUNNING, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
    "interrupt tool, no session":       (SessionStatus.RUNNING, {"outranked": N, "same rank": N, "out of reach": N, P: N}),
    "REST interrupt":                   (SessionStatus.RUNNING, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    # The binding switch (G3-01 / ticket 01a16a35-a788): a switch re-points what the session's next turn runs, so the caller drives the
    # creator's automation like a steer: the REST steer's rank rule. The URL's workspace is the scope this route reads in (a person's door,
    # like the REST steer): a session in another workspace, and one that is not there at all, both answer as an unknown id.
    "REST binding switch":              (SessionStatus.WAITING, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    # The session canvas (pivot P5 S6, the lead's ruling 1): a click drives the session like a steer, and an operator's batch or clear puts
    # input its agent reads into it, so all three follow the REST steer's rank rule; people keep their reach over every workspace.
    "REST canvas click":                (SessionStatus.WAITING, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    "REST canvas draw":                 (SessionStatus.WAITING, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    "REST canvas clear":                (SessionStatus.WAITING, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    # a DELETE ends a waiting session on the spot, and with ``?force=true`` flags a RUNNING one cancelled and publishes the cancel, before the row
    # goes: the cancel's rule (ticket 01a16d9f); a delivered delete is seen as the row gone, a refused one as the row untouched
    "REST delete":                      (SessionStatus.WAITING, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    "REST delete, forced":              (SessionStatus.RUNNING, {"outranked": N, "same rank": D, "out of reach": F, P: D}),
    "get session tool, in a session":   (SessionStatus.WAITING, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
    "read messages tool, in a session": (SessionStatus.WAITING, {"outranked": D, "same rank": D, "out of reach": N, P: D}),
}
SCENARIOS = ["outranked", "same rank", "out of reach", P]
TOOLS = {
    "cancel": "cancel_workspace_session", "resume": "resume_workspace_session", "pause": "pause_workspace_session",
    "interrupt": "interrupt_workspace_session", "get session": "get_workspace_session", "read messages": "read_workspace_session_messages",
}


def _ws_row(wid: str, access: Access) -> Workspace:
    """The workspace row a REST row's target session lives in (the session inherits its access, P2 Task 3)."""
    return Workspace(
        id=wid, template_id="tpl-1", provider_id="local-1", created_at=NOW,
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token=SecretStr("stub-token")), access=access,
    )


class _Slot(FakeSlot):
    """The on-disk slot, as the get tool uses it. The pause and resume tools never touch it: they write the session row (ticket 01a16d3c-9984)."""

    def __init__(self, session_id: str = "ses-t") -> None:
        super().__init__()
        self.session_id = session_id

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
    # The workspace rows the access rule (P2 T6) asks about: u-user's home is ws-1; ws-2 belongs to u-user only in ``from prime``, where the person
    # holds full on the target, and to u-ada otherwise, where the person holds no level and the target answers as one that does not exist.
    ws2_owner = "u-user" if scenario == P else "u-ada"
    for wid, owner in (("ws-1", "u-user"), ("ws-2", ws2_owner)):
        if await world.sp.get_storage(Workspace).get(wid) is not None:
            continue  # a REST row seeded the target's workspace with the access it tests
        await world.sp.get_storage(Workspace).create(Workspace(
            id=wid, name=wid, template_id="tpl", provider_id="local-1", created_at=NOW, phase="running",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
            access=Access.owned_by(owner),
        ))
    await seed_session(world, sid="ses-a", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER, created_at=NOW)
    workspace = "ws-2" if scenario in ("out of reach", P) else "ws-1"
    await seed_session(
        world, sid="ses-t", workspace_id=workspace, status=TABLE[path][0], turn_no=1, created_at=NOW,
        initiated_by=ADMIN if scenario == "outranked" else USER,
    )
    world.registry.remove("ws-nope")
    if path == "REST binding switch":
        # the switch's target must exist (the route 404s a missing target before it writes): the idle session switches to it at once
        await world.sp.get_storage(Agent).create(Agent(access=Access(public=True), 
            id="operator2", description="x", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[],
        ))
    if path.startswith("REST canvas"):
        await world.sp.get_storage(SessionCanvas).create(SessionCanvas(
            id="ses-t", workspace_id=workspace, rev=1,
            elements={"root": {"type": "Column", "props": {}, "children": ["go"]}, "go": {"type": "Button", "props": {"label": "Go"}}},
        ))
    return workspace


#: The REST canvas rows: (method, the path under the session, the body).
CANVAS_REST = {
    "REST canvas click": ("post", "/canvas/events", {"element": "go", "kind": "click"}),
    "REST canvas draw": ("post", "/canvas/ops", {"source": 'note = Text("hello")\nroot += [note]'}),
    "REST canvas clear": ("delete", "/canvas", None),
}


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
        # No user behind the token (access-control P2 T6): the call names no person, holds no level, and answers not-found.
        token = current_actor.set(Principal(type="user", id="u-mcp", display="mcp", role="user", source="local"))
        try:
            result = await dispatch_inner_tool(provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=name, arguments=arguments, ctx=None)
        finally:
            current_actor.reset(token)
    body = json.loads(result.output)
    return (D if not result.is_error else body["type"]), body


async def _drive(env: Env, path: str) -> tuple[str, dict]:
    if path in CANVAS_REST:
        method, tail, body = CANVAS_REST[path]
        r = await getattr(env.rest, method)(
            f"/v1/workspaces/{env.target_workspace}/sessions/ses-t{tail}", **({"json": body} if body is not None else {}),
        )
        return {200: D, 202: D, 403: F, 404: N}[r.status_code], r.json()
    if path.startswith("REST delete"):
        query = {"force": "true"} if path.endswith("forced") else None
        r = await env.rest.delete(f"/v1/workspaces/{env.target_workspace}/sessions/ses-t", params=query)
        return {204: D, 403: F, 404: N}[r.status_code], (r.json() if r.content else {})
    if path == "REST binding switch":
        r = await env.rest.post(
            f"/v1/workspaces/{env.target_workspace}/sessions/ses-t/binding",
            json={"kind": "agent", "agent_id": "operator2"},
        )
        return {200: D, 403: F, 404: N}[r.status_code], r.json()
    if path.startswith("REST"):
        verb = path.removeprefix("REST ")
        r = await env.rest.post(f"/v1/workspaces/{env.target_workspace}/sessions/ses-t/{verb}", json={})
        return {200: D, 204: D, 403: F, 404: N}[r.status_code], (r.json() if r.content else {})
    return await _tool(env, path, workspace=env.target_workspace, session="ses-t", in_session=path.endswith("in a session"))


async def _delivered(env: Env, path: str) -> bool:
    """Read off the session ROW, where every one of these paths writes (the pause and resume tools too, since ticket 01a16d3c-9984)."""
    row = await env.world.sessions.get("ses-t")
    if path in CANVAS_REST:
        canvas = await env.world.sp.get_storage(SessionCanvas).get("ses-t")
        if path == "REST canvas click":
            slot = env.world.registry.workspace(env.target_workspace).slot("ses-t")
            return canvas.event_seq == 1 and len(slot.appended) == 1 and slot.appended[0].startswith("<canvas_event")
        return canvas.rev == 2
    if path.startswith("REST delete"):
        return row is None
    if path.startswith(("cancel", "REST cancel")):
        return (row.status, row.ended_reason) == (SessionStatus.ENDED, "cancelled")
    if path == "REST resume":
        return row.status == SessionStatus.RUNNING  # the app's own scheduler queued it
    if path.startswith("resume"):
        return row.status == SessionStatus.RUNNING and env.world.scheduler.enqueued == ["ses-t"]
    if path.startswith(("pause", "REST pause")):
        return row.status == SessionStatus.PAUSED  # the target waits, so it pauses at once
    if path.startswith(("interrupt", "REST interrupt")):
        return row.interrupt_requested is True  # the target runs a turn, so the Stop is recorded
    if path == "REST binding switch":
        # the target waits idle, so a delivered switch is applied at once: the binding moved (with its snapshot), the epoch bumped, no
        # pending request left
        return (row.binding.kind, row.binding.agent_id) == ("agent", "operator2") and row.binding_epoch == 1 and row.pending_binding_switch is None
    return True  # get, read: the answer is the delivery


async def _canvas_of(env: Env) -> dict | None:
    canvas = await env.world.sp.get_storage(SessionCanvas).get("ses-t")
    return canvas.model_dump(mode="json") if canvas is not None else None


async def _check(env: Env, path: str, scenario: str) -> None:
    before = (await env.world.sessions.get("ses-t")).model_dump(mode="json")
    canvas_before = await _canvas_of(env)

    outcome, body = await _drive(env, path)

    assert outcome == TABLE[path][1][scenario], (path, scenario, body)
    if outcome == D:
        assert await _delivered(env, path), (path, scenario)
        return
    assert (await env.world.sessions.get("ses-t")).model_dump(mode="json") == before, f"{path} wrote to the target it refused ({scenario})"
    assert env.world.scheduler.enqueued == [], f"{path} armed the target it refused ({scenario})"
    assert await _canvas_of(env) == canvas_before, f"{path} wrote the canvas it refused ({scenario})"
    if outcome == N:
        if path.startswith("REST"):
            # The access gate answers the session's missing-row text: an out-of-access target is indistinguishable from a missing id (spec 9).
            assert body.get("detail") == "Session 'ses-t' does not exist", (body,)
            return
        # The out-of-access answer is exactly what an unknown workspace answers (asked with no calling session, past the access rule): the same
        # ``Workspace '<id>' does not exist`` words, the id masked.
        _, unknown = await _tool(env, path, workspace="ws-nope", session="ses-nobody", in_session=False)
        seen = json.dumps(body).replace(env.target_workspace, "<ws>").replace("ses-t", "<id>")
        assert seen == json.dumps(unknown).replace("ws-nope", "<ws>").replace("ses-nobody", "<id>"), (body, unknown)
    if outcome == F:
        if path.startswith("REST"):
            # The reader's refusal names the level it needs (the 403 side of spec 9); the tool rows' wording is asserted by their own tests.
            assert body.get("detail") == "Session 'ses-t' needs full access", (body,)


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("path", [p for p in TABLE if not p.startswith("REST")])
async def test_every_tool_that_acts_on_a_session_obeys_the_rule_table(path, scenario):
    env = Env(world=_world())
    env.target_workspace = await _seed(env.world, path, scenario)
    await _check(env, path, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("path", [p for p in TABLE if p.startswith("REST")])
async def test_the_rest_routes_obey_the_rule_table(app, client, fake_storage_provider, path, scenario):
    world = _world(fake_storage_provider)
    app.state.workspace_registry.get_workspace = world.registry.get_workspace  # type: ignore[method-assign]
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    carol_id = await _user_id(app, "carol")
    # The REST rows are judged by the workspace access the session inherits (P2 Task 3): the target's workspace row carries it.
    await fake_storage_provider.get_storage(Workspace).create(_ws_row(
        "ws-2" if scenario in ("out of reach", P) else "ws-1",
        _access_for({"outranked": "admin", "same rank": "carol", "out of reach": "carol read", P: "carol"}[scenario], carol_id,
                    await _user_id(app, "testuser")),
    ))
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
    pytest.param("ws-1", "ws-2", False, id="another workspace, one the person holds no level on"),
    pytest.param("ws-1", "ws-1", True, id="the person's own workspace"),
    pytest.param("ws-prime", "ws-1", True, id="from prime: the person's level decides, not the workspace"),
])
async def test_create_workspace_session_from_a_session_acts_only_where_the_person_holds_full(own, target, reached):
    """A session outside prime that could create a session in prime would reach every workspace from there, so the create follows the run's
    person (access-control P2 T6): a workspace the person holds no level on answers exactly as a workspace that does not exist; one they hold
    full on (from any workspace, prime included) goes on (here it stops at the agent it names, which does not exist)."""
    sp = _FakeStorageProvider()
    await sp.set_default_workspace_id("ws-prime")
    # u-user's home is ws-1; ws-2 belongs to u-ada (the run's person, u-user, holds no level on it).
    for wid, access in (("ws-1", Access.owned_by("u-user")), ("ws-2", Access.owned_by("u-ada")), ("ws-prime", Access())):
        await sp.get_storage(Workspace).create(Workspace(
            id=wid, name=wid, template_id="local-default", provider_id="local", created_at=NOW, phase="running",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
            access=access,
        ))
    await sp.get_storage(Agent).create(Agent(access=Access(public=True), id="somebody", description="x", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]))
    ctx = ToolContext(tool_call_id="c1", session_id="ses-a", workspace_id=own, initiated_by=USER)

    body = await _create(sp, target, ctx)
    unknown = await _create(sp, "ws-nope", None)

    assert unknown == {"type": "not-found", "message": "Workspace 'ws-nope' does not exist"}
    if reached:
        assert body["type"] != "not-found", body
    else:
        assert body == {"type": "not-found", "message": f"Workspace {target!r} does not exist"}


@pytest.mark.parametrize("verb", ["cancel", "resume", "pause", "interrupt"])
async def test_cancel_resume_pause_and_interrupt_from_a_session_act_whatever_the_sessions_recorded_creator(verb):
    """The creator is no longer consulted (access-control P2 T6): the target was started by an admin, the run's person holds full on the
    workspace, and the call acts."""
    path = f"{verb} tool, in a session"
    env = Env(world=_world())
    env.target_workspace = await _seed(env.world, path, "outranked")

    outcome, body = await _tool(env, path, workspace="ws-1", session="ses-t", in_session=True)

    assert outcome == D, body
    assert await _delivered(env, path), (verb, body)


# ---- pause and Stop follow the rank rule, person by person (ticket 01a16d3c-a8bc) ------------------------------------------------------------
#
# A pause stops a run at its creator's rank and a Stop (interrupt) cuts its turn short, so both follow the cancel's rule on every surface: the REST
# routes (over HTTP, through the auth middleware, as a logged-in account or with an API token, which ranks as its owner), the tools over MCP (no
# calling session, the request's actor on ``current_actor``) and the tools from another session (a run, which is ranked by its principal and never
# gets the own-session rule). A refused call answers 403 ``forbidden_rank`` (REST) or ``type=forbidden`` (a tool), and nothing moves: the row, the
# bus and the queue are as they were.

#: verb -> (the target's status, what a delivered call leaves on the row).
CONTROLS: dict[str, tuple[SessionStatus, Callable[[WorkspaceSession], bool]]] = {
    "pause": (SessionStatus.WAITING, lambda row: row.status == SessionStatus.PAUSED),
    "interrupt": (SessionStatus.RUNNING, lambda row: row.interrupt_requested is True),
}


@pytest.mark.parametrize("who", list(PERSONS))
@pytest.mark.parametrize("verb", list(CONTROLS))
async def test_a_persons_pause_or_stop_over_rest_follows_the_workspace_access(app, client, fake_storage_provider, verb, who):
    access_spec, asker, accepted = PERSONS[who]
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    carol_id = await _user_id(app, "carol")
    await fake_storage_provider.get_storage(Workspace).create(
        _ws_row("ws-r", _access_for(access_spec, carol_id, await _user_id(app, "testuser"))),
    )
    status, moved = CONTROLS[verb]
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="ses-p", workspace_id="ws-r", binding=AgentSessionBinding(agent_id="operator"), status=status, turn_no=1, created_at=NOW,
        initiated_by=USER,
    ))
    before = (await sessions.get("ses-p")).model_dump(mode="json")
    spy = _Spy(app.state.event_bus)
    route = f"/v1/workspaces/ws-r/sessions/ses-p/{verb}"

    if asker.endswith("token"):
        async with await _token_client(app, carol_id if asker.startswith("carol") else await _user_id(app, "testuser")) as tok:
            r = await tok.post(route, json={})
    elif asker == "carol":
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
            assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
            r = await carol.post(route, json={})
    else:
        r = await client.post(route, json={})

    if not accepted:
        if access_spec == "carol read":
            assert r.status_code == 403, f"{who} could {verb}: {r.text}"
            assert r.json()["detail"] == "Session 'ses-p' needs full access"
        else:
            assert r.status_code == 404, f"{who} could {verb}: {r.text}"
            assert r.json()["detail"] == "Session 'ses-p' does not exist"
        assert (await sessions.get("ses-p")).model_dump(mode="json") == before, f"a refused {verb} wrote to the session"
        assert spy.keys == [], f"a refused {verb} reached the event bus"
        return
    assert r.status_code in (200, 204), f"{who} could not {verb}: {r.text}"
    assert moved(await sessions.get("ses-p")), f"{who}'s {verb} did not land"


# ---- DELETE follows the cancel's access gate, person by person (ticket 01a16d9f) ---------------------------------------------------------------
#
# ``DELETE /v1/workspaces/{wid}/sessions/{sid}`` ends a CREATED, WAITING or PAUSED session on the spot and, with ``?force=true``, flags a RUNNING one
# cancelled and publishes ``session:{sid}:cancel`` before the row goes: a cancel, so it follows the cancel's gate, decided before anything else as
# the cancel route decides it (an ended session too). A refused delete answers the session's missing-row 404 (a stranger) or the 403 that names the
# level (a reader) and changes nothing: the row, the session's pending external tool call (a delete resolves it as cancelled), the bus.

#: the target -> (its status, the delete's query).
DELETES: dict[str, tuple[SessionStatus, dict[str, str]]] = {
    "a waiting session": (SessionStatus.WAITING, {}),
    "a running session, forced": (SessionStatus.RUNNING, {"force": "true"}),
    "an ended session": (SessionStatus.ENDED, {}),
}


@pytest.mark.parametrize("who", list(PERSONS))
@pytest.mark.parametrize("target", list(DELETES))
async def test_a_persons_delete_over_rest_follows_the_workspace_access(app, client, fake_storage_provider, target, who):
    from primer.model.external_tool import ExternalToolCall

    access_spec, asker, accepted = PERSONS[who]
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    carol_id = await _user_id(app, "carol")
    await fake_storage_provider.get_storage(Workspace).create(
        _ws_row("ws-r", _access_for(access_spec, carol_id, await _user_id(app, "testuser"))),
    )
    status, query = DELETES[target]
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="ses-p", workspace_id="ws-r", binding=AgentSessionBinding(agent_id="operator"), status=status, turn_no=1, created_at=NOW,
        initiated_by=USER, ended_reason="completed" if status == SessionStatus.ENDED else None,
        ended_at=NOW if status == SessionStatus.ENDED else None,
    ))
    calls = fake_storage_provider.get_storage(ExternalToolCall)
    await calls.create(ExternalToolCall(id="etool-p", session_id="ses-p", tool_call_id="tc-p", tool_name="client_lookup", created_at=NOW))
    before = (await sessions.get("ses-p")).model_dump(mode="json")
    spy = _Spy(app.state.event_bus)
    route = "/v1/workspaces/ws-r/sessions/ses-p"

    if asker.endswith("token"):
        async with await _token_client(app, carol_id if asker.startswith("carol") else await _user_id(app, "testuser")) as tok:
            r = await tok.delete(route, params=query)
    elif asker == "carol":
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
            assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
            r = await carol.delete(route, params=query)
    else:
        r = await client.delete(route, params=query)

    if not accepted:
        if access_spec == "carol read":
            assert r.status_code == 403, f"{who} could delete {target}: {r.text}"
            assert r.json()["detail"] == "Session 'ses-p' needs full access"
        else:
            assert r.status_code == 404, f"{who} could delete {target}: {r.text}"
            assert r.json()["detail"] == "Session 'ses-p' does not exist"
        assert (await sessions.get("ses-p")).model_dump(mode="json") == before, "a refused delete wrote to the session"
        assert (await calls.get("etool-p")).status == "pending", "a refused delete resolved the session's external tool call"
        assert spy.keys == [], "a refused delete reached the event bus"
        return
    assert r.status_code == 204, f"{who} could not delete {target}: {r.text}"
    assert await sessions.get("ses-p") is None, f"{who}'s delete did not remove the session"
    assert (await calls.get("etool-p")).status == "cancelled"
    if query:
        assert "session:ses-p:cancel" in spy.keys, "the forced delete of a running session published no cancel"


# ---- The binding switch follows the steer's access gate, person by person (G3-01 / ticket 01a16a35-a788) ------------------------------------------
#
# ``POST /v1/workspaces/{wid}/sessions/{sid}/binding`` re-points what the session's next turn runs, so the caller drives the session's
# automation the way a steer does: the request's actor at or above full on the session's workspace (an admin never refused) acts, a stranger
# gets the session's missing-row 404 and a reader the 403 that names the level, and nothing moves. The route's door is a person's (the auth
# middleware; it has no tool form), and the workspace the URL names is the scope it reads in once the gate admits the caller: a session in
# another workspace, and a session that is not there at all, both answer exactly like an unknown id. The in-session door (``system__switch_binding``)
# acts on the calling session itself only, so it is in reach and in rank by construction.


@pytest.mark.parametrize("who", list(PERSONS))
async def test_a_persons_binding_switch_over_rest_follows_the_workspace_access(app, client, fake_storage_provider, who):
    access_spec, asker, accepted = PERSONS[who]
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    carol_id = await _user_id(app, "carol")
    world = _world(fake_storage_provider)
    app.state.workspace_registry.get_workspace = world.registry.get_workspace  # type: ignore[method-assign]
    await fake_storage_provider.get_storage(Workspace).create(
        _ws_row("ws-b", _access_for(access_spec, carol_id, await _user_id(app, "testuser"))),
    )
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="ses-b", workspace_id="ws-b", binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.WAITING, turn_no=1,
        created_at=NOW, initiated_by=USER,
    ))
    await fake_storage_provider.get_storage(Agent).create(Agent(access=Access(public=True), 
        id="operator2", description="x", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[],
    ))
    before = (await sessions.get("ses-b")).model_dump(mode="json")
    spy = _Spy(app.state.event_bus)
    route = "/v1/workspaces/ws-b/sessions/ses-b/binding"
    body = {"kind": "agent", "agent_id": "operator2"}

    if asker.endswith("token"):
        async with await _token_client(app, carol_id if asker.startswith("carol") else await _user_id(app, "testuser")) as tok:
            r = await tok.post(route, json=body)
    elif asker == "carol":
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
            assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
            r = await carol.post(route, json=body)
    else:
        r = await client.post(route, json=body)

    if not accepted:
        if access_spec == "carol read":
            assert r.status_code == 403, f"{who} could switch the binding: {r.text}"
            assert r.json()["detail"] == "Session 'ses-b' needs full access"
        else:
            assert r.status_code == 404, f"{who} could switch the binding: {r.text}"
            assert r.json()["detail"] == "Session 'ses-b' does not exist"
        assert (await sessions.get("ses-b")).model_dump(mode="json") == before, "a refused switch wrote to the session"
        assert spy.keys == [], "a refused switch reached the event bus"
        return
    assert r.status_code == 200, f"{who} could not switch the binding: {r.text}"
    row = await sessions.get("ses-b")
    assert (row.binding.kind, row.binding.agent_id) == ("agent", "operator2")
    assert row.binding_epoch == 1
    assert row.pending_binding_switch is None


async def test_a_binding_switch_out_of_its_workspaces_url_scope_answers_as_an_unknown_id(app, client, fake_storage_provider):
    """The route's reach is the workspace the URL names (it has no calling session): a session that lives in another workspace is answered
    as an unknown id once the access gate admits the caller (carol owns the workspace the session lives in); a stranger to it gets the
    session's missing-row text - which is what the unknown id gets too (spec 9). Either way the answer changes nothing."""
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    carol_id = await _user_id(app, "carol")
    await fake_storage_provider.get_storage(Workspace).create(
        _ws_row("ws-2", _access_for("carol", carol_id, await _user_id(app, "testuser"))),
    )
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(WorkspaceSession(
        id="ses-b", workspace_id="ws-2", binding=AgentSessionBinding(agent_id="planner"), status=SessionStatus.WAITING,
        created_at=NOW, initiated_by=PrincipalRef(type="user", id=carol_id, display="carol", role="user", source="local"),
    ))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
        assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
        body = {"kind": "agent", "agent_id": "planner"}
        r_out = await carol.post("/v1/workspaces/ws-1/sessions/ses-b/binding", json=body)
        r_unknown = await carol.post("/v1/workspaces/ws-1/sessions/ses-nobody/binding", json=body)
        assert r_out.status_code == r_unknown.status_code == 404, (r_out.status_code, r_unknown.status_code)
        assert r_unknown.json()["detail"] == "Session 'ses-nobody' does not exist", r_unknown.json()
        assert r_out.json()["detail"].replace("ses-b", "<id>") == r_unknown.json()["detail"].replace("ses-nobody", "<id>")
        row = await sessions.get("ses-b")
        assert (row.binding.kind, row.binding.agent_id) == ("agent", "planner") and row.binding_epoch == 0, "a refused switch wrote to the session"


#: who -> (the session's creator, the caller: a request's actor over MCP, or a run's principal; what it answers). The target sits in ws-1, owned
#: by u-user, with u-ada granted full and u-read read (access-control P2 T6): the creator (an admin in most rows) is no longer consulted.
ADA_THEN = PrincipalRef(type="user", id="u-ada", display="ada", role="admin", source="local")
SELF = "the session's own run, holding full"
TOOL_CALLERS: dict[str, tuple[PrincipalRef, Principal | PrincipalRef, str]] = {
    "MCP: a user with no level on the workspace": (ADMIN, Principal(type="user", id="u-mcp", display="mcp", role="user", source="local"), N),
    "MCP: a reader on the workspace": (
        ADMIN, Principal(type="user", id="u-read", display="read", role="user", source="local", user_id="u-read"), F,
    ),
    "MCP: an admin": (ADMIN, Principal(type="user", id="u-root", display="root", role="admin", source="local"), D),
    "MCP: a user holding full": (ADMIN, Principal(type="user", id="u-user", display="user", role="user", source="local", user_id="u-user"), D),
    "MCP: a demoted admin holding full": (
        ADA_THEN, Principal(type="user", id="u-ada", display="ada", role="user", source="local", user_id="u-ada"), D,
    ),
    "MCP: a user's token holding full": (
        ADMIN, Principal(type="api_token", id="at-user", display="automation", role="user", source="internal", user_id="u-user"), D,
    ),
    "a run holding full": (ADMIN, ADA_REF, D),
    "a run with no level": (ADMIN, PrincipalRef(type="user", id="u-mcp", display="mcp", role="user", source="local"), N),
    # the review of #904: the session's OWN run acts on itself (the level on the workspace decides, not the recorded creator)
    SELF: (ADA_THEN, ADA_REF, D),
}


@pytest.mark.parametrize("who", list(TOOL_CALLERS))
@pytest.mark.parametrize("verb", list(CONTROLS))
async def test_the_pause_and_interrupt_tools_act_when_the_person_holds_full_on_the_workspace(verb, who):
    """Access version of the rank rule (P2 T6): the pause and the Stop need full on the target's workspace for the caller's person; a workspace
    with no level answers not-found, a read level answers forbidden, and nothing moves on a refusal."""
    creator, caller, expected = TOOL_CALLERS[who]
    status, moved = CONTROLS[verb]
    world = _world()
    await world.sp.get_storage(Workspace).create(Workspace(
        id="ws-1", name="ws-1", template_id="tpl", provider_id="local-1", created_at=NOW, phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
        access=Access(owners=["u-user"], grants={"u-ada": "full", "u-read": "read"}),
    ))
    await seed_session(world, sid="ses-a", workspace_id="ws-1", binding=AgentSessionBinding(agent_id="planner"), initiated_by=USER, created_at=NOW)
    await seed_session(world, sid="ses-t", workspace_id="ws-1", status=status, turn_no=1, created_at=NOW, initiated_by=creator)
    before = (await world.sessions.get("ses-t")).model_dump(mode="json")
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine, event_bus=world.bus,
    )
    arguments = {"workspace_id": "ws-1", "session_id": "ses-t"}
    tool = f"{verb}_workspace_session"
    if isinstance(caller, Principal):
        token = current_actor.set(caller)
        try:
            result = await dispatch_inner_tool(provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=tool, arguments=arguments, ctx=None)
        finally:
            current_actor.reset(token)
    else:
        ctx = ToolContext(tool_call_id="call-1", session_id="ses-t" if who == SELF else "ses-a", workspace_id="ws-1", initiated_by=caller)
        result = await dispatch_inner_tool(provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=tool, arguments=arguments, ctx=ctx)
    body = json.loads(result.output)

    if expected == N:
        assert (result.is_error, body) == (True, {"type": N, "message": "Workspace 'ws-1' does not exist"}), body
    elif expected == F:
        assert (result.is_error, body) == (True, {"type": F, "message": "Workspace 'ws-1' needs full access"}), body
    else:
        assert not result.is_error, body
        assert moved(await world.sessions.get("ses-t")), f"{who}'s {verb} did not land"
    if expected != D:
        assert (await world.sessions.get("ses-t")).model_dump(mode="json") == before, f"a refused {verb} wrote to the session"
        assert world.bus.published == [], f"a refused {verb} reached the event bus"


# ---- The workspace tools (ticket 01a16ad1) ----------------------------------------------------------------------------------------------
#
# The tools that name a WORKSPACE: deleting it, reading its row, draining its tap, reading its log and its files, writing and deleting a file. The
# access-control P2 T6 conversion: the run's person acts, and must hold the level the tool needs on the named workspace (owner for the delete,
# full for the writes, read for the reads); a workspace it holds no level on answers as one that does not exist. Every in-session row goes through
# the real tool manager twice: the tool by its own scoped name (an agent that lists the workspaces toolset), and ``system__call_tool`` (the door
# every agent has). The no-session row is the MCP endpoint's own call (``invoke_one`` with the request's actor on ``current_actor``), whose user
# here is u-ada.

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
    """Three workspaces on one provider (``ws-prime`` is the default, owned by u-ada like ws-2; ws-1 is u-user's), the real ``WorkspaceRegistry``
    over them, a calling session in each, and the session ``ses-t`` in ws-2 whose transcript the tap drains. The levels the caller's person
    holds decide the tool rows (access-control P2 T6): u-user holds no level on ws-2, u-ada holds it."""
    sp = _FakeStorageProvider()
    await sp.set_default_workspace_id("ws-prime")
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(id="local-1", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/nonexistent")),
    )
    for wid, access in (
        ("ws-1", Access.owned_by("u-user")),
        ("ws-2", Access(owners=["u-ada"], grants={"u-bob": "full"})),
        ("ws-prime", Access.owned_by("u-ada")),
    ):
        await sp.get_storage(Workspace).create(Workspace(
            id=wid, name=wid, template_id="tpl", provider_id="local-1", created_at=NOW, phase="running",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
            access=access,
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
#: The MCP endpoint's own call (``invoke_one``, no context): the token's user (u-ada, who holds the levels the ``no session`` rows act with; the rule
#: table's own ``_tool`` asks the doors as a token with no user behind it, which holds none - access-control P2 T6).
MCP_USER = Principal(type="user", id="u-mcp", display="mcp", role="user", source="local", user_id="u-ada")
#: The person each in-session caller acts as (access-control P2 T6): u-user's home is ws-1; the other callers act as u-ada, who holds full on
#: ws-2 and ws-prime.
PERSONS_FOR_OWN = {"ws-1": USER, "ws-2": ADA_REF, "ws-prime": ADA_REF}


async def _call(
    world: WsWorld, toolset: str, tool: str, arguments: dict, *, own: str | None, door: str, run_as: PrincipalRef | None = None,
    actor: Principal = MCP_USER,
) -> tuple[str, dict]:
    """``toolset``'s ``tool`` from a session in ``own`` (its run ranked ``run_as``) through ``door``: the tool by its scoped name in the real
    tool manager, or ``system__call_tool``. With ``own`` None it is the MCP endpoint's own call (``invoke_one``, no context, ``actor`` on
    ``current_actor``)."""
    if own is not None and run_as is None:
        run_as = PERSONS_FOR_OWN[own]  # the person the calling session's workspace stands for (access-control P2 T6)
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


async def _ws_call(world: WsWorld, tool: str, target: str, *, own: str | None, door: str, run_as: PrincipalRef = USER) -> tuple[str, dict]:
    """``tool`` on ``target``, from a session in ``own`` through ``door``, or with no calling session (``own`` None) as the MCP endpoint calls it."""
    return await _call(world, "workspaces", tool, _args(tool, target), own=own, door=door, run_as=run_as)


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
    """The person behind the call acts (access-control P2 T6): a workspace it holds no level on answers exactly as an unknown one and changes
    nothing; one it holds a level on is acted on whatever workspace the calling session sits in."""
    world = await _ws_world()
    own, expected = WS_CALLERS[who]
    before = await world.snapshot()

    outcome, body = await _ws_call(world, tool, "ws-2", own=own, door=door, run_as=PERSONS_FOR_OWN[own] if own else USER)

    assert outcome == expected, (tool, who, door, body)
    if outcome == D:
        assert await _ws_delivered(world, tool, body), (tool, who, door, body)
        return
    assert await world.snapshot() == before, f"{tool} changed the workspace it refused ({who}, {door})"
    # The out-of-access answer is exactly what an unknown workspace answers (asked as a person with no level: the MCP token's user holds no level
    # on ws-nope).
    _, unknown = await _ws_call(world, tool, "ws-nope", own=None, door="MCP")
    assert json.dumps(body).replace("ws-2", "<ws>") == json.dumps(unknown).replace("ws-nope", "<ws>"), (body, unknown)


@pytest.mark.parametrize("door", DOORS)
async def test_delete_workspace_from_outside_prime_naming_prime_answers_as_an_unknown_workspace(door):
    """Prime belongs to another person (access-control P2 T6): the caller's person holds no level on it and is told it does not exist (not that
    it is reserved), and nothing is torn down."""
    world = await _ws_world()
    before = await world.snapshot()

    outcome, body = await _ws_call(world, "delete_workspace", "ws-prime", own="ws-1", door=door)

    assert (outcome, body) == (N, {"type": N, "message": "Workspace 'ws-prime' does not exist"})
    assert await world.snapshot() == before


@pytest.mark.parametrize(("own", "door", "listed"), [
    pytest.param("ws-1", "tool manager", ["ws-1"], id="u-user's home, by name"),
    pytest.param("ws-1", "system__call_tool", ["ws-1"], id="u-user's home, through call_tool"),
    pytest.param("ws-prime", "system__call_tool", ["ws-2", "ws-prime"], id="u-ada's levels"),
    pytest.param(None, "MCP", ["ws-2", "ws-prime"], id="no calling session (MCP), the token's user"),
])
async def test_list_workspaces_lists_only_what_the_person_holds_a_level_on(own, door, listed):
    """The finder of workspaces follows the rule of ``find_sessions`` (access-control P2 T6): it lists the workspaces the caller's person holds a
    level on, so a workspace id it cannot read is never shown to it (``get_workspace`` answers such an id as unknown)."""
    world = await _ws_world()

    outcome, body = await _ws_call(world, "list_workspaces", "", own=own, door=door, run_as=PERSONS_FOR_OWN[own] if own else USER)

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
    await world.sp.get_storage(Channel).create(Channel(access=Access(public=True), 
        id="ch-1", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM, external_id="777", config=TelegramChannelConfig(),
    ))
    store = world.sp.get_storage(Workspace)
    await store.update((await store.get("ws-2")).model_copy(update={"reply_binding": WorkspaceChannelLink(channel_id="ch-old")}))
    return world


def _reply_args(tool: str, target: str, **extra) -> dict:
    return {**{k: (target if v == W else v) for k, v in REPLY_DOORS[tool].items()}, **extra}


@pytest.mark.parametrize(("tool", "who", "door"), _rows(REPLY_DOORS))
async def test_the_reply_binding_tools_reach_only_what_the_caller_reaches(tool, who, door):
    """F2, access version (P2 T6): ``set_reply_binding`` and ``clear_reply_binding`` name a workspace like the workspaces tools do, and need full
    on it for the caller's person: a workspace the person holds no level on answers exactly as one that does not exist."""
    world = await _reply_world()
    own, expected = WS_CALLERS[who]
    before = await world.snapshot()

    outcome, body = await asyncio.wait_for(_call(world, "system", tool, _reply_args(tool, "ws-2"), own=own, door=door, run_as=PERSONS_FOR_OWN[own] if own else USER), 30)

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
#: Who calls -> the reach recorded on what it saves: none of them records one any more (access-control P2 T6: the owner's level on the target decides).
RECORDED = {"own workspace": None, P: None, "no session": None}


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
    """F1, access version (P2 T6): a save must be one the owner (the caller's person) holds full on the target workspace. A target the person
    holds no full on answers exactly as an unknown workspace or session does (from the same caller) and stores nothing; a save the person holds
    full on goes on and records the reach it was saved with."""
    world = await _seed_world_for_subscriptions()
    toolset, tool, arguments, (target, unknown) = SUB_DOORS[door_name]
    own, expected = WS_CALLERS[who]
    before = await _subscriptions(world)

    outcome, body = await asyncio.wait_for(_call(world, toolset, tool, arguments(target), own=own, door=door, run_as=PERSONS_FOR_OWN[own] if own else USER), 30)

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
    """F1, access version (P2 T6): updating a subscription saves it as the caller (its owner, and now its reach): the caller's person must hold
    full on the target, and a subscription whose target is outside the caller's reach answers exactly as a subscription id that does not exist
    (``subscription_not_found``; review of #874, C1) and changes nothing. A save the person holds full on goes on and records the caller's reach."""
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

    outcome, body = await asyncio.wait_for(_call(world, "trigger", "update_subscription", _update("sb-p"), own=own, door=door, run_as=PERSONS_FOR_OWN[own] if own else USER), 30)

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
    return Graph(access=Access(public=True), 
        id="g-1", description="begin -> end", nodes=[_BeginNode(id="begin"), _EndNode(id="end")],
        edges=[_StaticEdge(from_node="begin", to_node="end")],
    )


async def _fire_world(ws2_owner: str = "u-ada") -> World:
    """Three workspaces (``ws-prime`` the default), the subscription owner's account, an agent and a graph to start, ``ses-t`` (in ws-2) to steer
    and ``ses-pk`` (in ws-2) parked on a trigger subscription. ws-2 belongs to ``ws2_owner``: the fire is gated on the subscription OWNER's level on
    the target (access-control P2 T6), not on the reach recorded on the row."""
    world = World(sp=_FakeStorageProvider(), registry=_FireRegistry(), scheduler=FakeScheduler(), engine=FakeEngine(), bus=FakeBus())
    await world.sp.set_default_workspace_id("ws-prime")
    for wid in ("ws-1", "ws-2", "ws-prime"):
        await world.sp.get_storage(Workspace).create(Workspace(
            id=wid, name=wid, template_id="tpl", provider_id="local-1", created_at=NOW, phase="running",
            runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
            access=Access.owned_by(ws2_owner) if wid == "ws-2" else Access(),
        ))
    await world.sp.get_storage(User).create(User(id="u-user", username="user", created_at=NOW, role="user"))
    await world.sp.get_storage(Agent).create(Agent(access=Access(public=True), id="planner", description="x", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]))
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
@pytest.mark.parametrize(("holder", "fed"), [
    pytest.param("u-user", True, id="the owner's person holds full on the target"),
    pytest.param("u-ada", False, id="the owner's person holds no level on the target"),
])
async def test_a_fire_never_starts_or_feeds_a_session_the_owner_holds_no_full_on(kind, holder, fed):
    """F1 at fire time, access version (P2 T6): the worker's trigger handler fires the trigger, and a fire is gated on the owner recorded on the
    row holding full on the target's workspace, whatever the reach recorded on the row says. The owner holding full starts and feeds the target;
    the other is refused and logged (``target_out_of_reach``, a failed delivery on the subscription) and never starts or feeds a session."""
    world = await _fire_world(holder)
    config, payload = FIRE_KINDS[kind]
    await world.sp.get_storage(Subscription).create(Subscription.model_validate({
        "id": "sb-fire", "trigger_id": "tr-fire", "config": config.model_dump(mode="json"), "payload_template": payload, "parallelism": "queue",
        "created_at": NOW.isoformat(), "owner": USER.model_dump(mode="json"), "reach": None,
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

    assert await _what_the_fire_did(world, kind, before) is fed, (kind, holder)
    sub = await world.sp.get_storage(Subscription).get("sb-fire")
    if fed:
        assert sub is None or sub.last_fire_error is None, sub.last_fire_error
        return
    assert json.loads(sub.last_fire_error)["code"] == "target_out_of_reach", sub.last_fire_error
    assert [o.success for o in released] == [False], "a refused fire is a failed delivery, not a clean run"


# -- delete_workspace and the owner rule (F3) --------------------------------------------------------------------------------------------------
#
# The tool's delete and the REST delete need owner on the workspace (access-control P2, where the cancel's rank rule and the scan of the
# workspace's open sessions lived for them).


async def _admin_session_in_ws2(world: WsWorld, *, status: SessionStatus = SessionStatus.WAITING) -> None:
    await world.sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="ses-adm", workspace_id="ws-2", binding=AgentSessionBinding(agent_id="planner"), status=status, created_at=NOW, initiated_by=ADMIN,
    ))


_OWNER_NEEDS = "Workspace 'ws-2' needs owner access"
_OWNER_NOT_FOUND = "Workspace 'ws-2' does not exist"


@pytest.mark.parametrize(("who", "door", "expected"), [
    pytest.param("a full grantee", "tool manager", (F, {"type": F, "message": _OWNER_NEEDS}), id="full, by name"),
    pytest.param("a full grantee", "system__call_tool", (F, {"type": F, "message": _OWNER_NEEDS}), id="full, through call_tool"),
    pytest.param("no level", "tool manager", (N, {"type": N, "message": _OWNER_NOT_FOUND}), id="no level, by name"),
    pytest.param("no level", "system__call_tool", (N, {"type": N, "message": _OWNER_NOT_FOUND}), id="no level, through call_tool"),
])
async def test_delete_workspace_is_refused_when_the_person_holds_below_owner(who, door, expected):
    """F3, access version (P2 T6): the delete needs owner on the workspace, and the open sessions no longer rank it (an admin's session is open
    through all of it): full is refused in the delete's own words, no level as a workspace that does not exist, and nothing is torn down."""
    world = await _ws_world()
    await _admin_session_in_ws2(world)
    before = await world.snapshot()
    caller = BOB if who.startswith("a full") else USER

    outcome, body = await asyncio.wait_for(_ws_call(world, "delete_workspace", "ws-2", own="ws-2", door=door, run_as=caller), 30)

    assert (outcome, body) == expected, (who, door, body)
    assert await world.snapshot() == before


@pytest.mark.parametrize(("case", "own", "door", "run_as"), [
    pytest.param("an admin run", "ws-2", "system__call_tool", ADMIN, id="an admin-rank session in ws-2"),
    pytest.param("an admin over MCP", None, "MCP", None, id="an admin over MCP"),
    pytest.param("the owner", "ws-2", "tool manager", ADA_REF, id="the workspace's owner, the admin's session open"),
])
async def test_delete_workspace_goes_on_when_the_person_holds_owner(case, own, door, run_as):
    """F3, access version (P2 T6): the delete ends every open session in the workspace without asking the sessions' ranks: an owner (or an admin)
    deletes it while an admin's session is still open in it."""
    world = await _ws_world()
    await _admin_session_in_ws2(world)
    admin = Principal(type="user", id="u-root", display="root", role="admin", source="local")

    outcome, body = await asyncio.wait_for(_call(
        world, "workspaces", "delete_workspace", {"id": "ws-2"}, own=own, door=door,
        run_as=run_as if run_as is not None else USER, actor=admin,
    ), 30)

    assert outcome == D, body
    assert await world.sp.get_storage(Workspace).get("ws-2") is None


async def test_the_rest_delete_of_a_workspace_is_decided_by_its_access(app, client, fake_storage_provider):
    """F3 over REST, access edition (P2 Task 2/3): the gate is the workspace's OWNER access, not a rank scan of its open sessions. The owner
    deletes even while an admin's session is open in it (the destroy ends that session, as it always has); a stranger to the workspace gets
    its missing-row 404 and nothing is torn down; an admin bypasses the gate."""
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
    carol_id = await _user_id(app, "carol")
    # carol owns ws-2: her delete lands although an admin's session is open in it (the access gate no longer scans sessions).
    granted = await client.put("/v1/workspaces/ws-2/access", json={"owners": [carol_id], "grants": {}})
    assert granted.status_code == 200, granted.text
    r = await client.post("/v1/admin/users", json={"username": "dave", "password": "davepw12"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as dave:
        assert (await dave.post("/v1/auth/login", json={"username": "dave", "password": "davepw12"})).status_code == 200
        refused = await asyncio.wait_for(dave.delete("/v1/workspaces/ws-2"), 30)
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"] == "Workspace 'ws-2' does not exist"
    assert await fake_storage_provider.get_storage(Workspace).get("ws-2") is not None
    assert (await fake_storage_provider.get_storage(WorkspaceSession).get("ses-adm")).status == SessionStatus.WAITING
    assert "ws-2" in backend.live

    r = await client.post("/v1/admin/users", json={"username": "erin", "password": "erinpw12"})
    assert r.status_code == 201, r.text
    erin_id = await _user_id(app, "erin")
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as erin:
        assert (await erin.post("/v1/auth/login", json={"username": "erin", "password": "erinpw12"})).status_code == 200
        await client.put("/v1/workspaces/ws-2/access", json={"owners": [carol_id], "grants": {erin_id: "full"}})
        refused = await asyncio.wait_for(erin.delete("/v1/workspaces/ws-2"), 30)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "Workspace 'ws-2' needs owner access"
    assert await fake_storage_provider.get_storage(Workspace).get("ws-2") is not None

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
        assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
        deleted = await asyncio.wait_for(carol.delete("/v1/workspaces/ws-2"), 30)

    assert deleted.status_code == 204, deleted.text
    assert await fake_storage_provider.get_storage(Workspace).get("ws-2") is None
    ended = await fake_storage_provider.get_storage(WorkspaceSession).get("ses-adm")
    assert ended is not None and ended.status == SessionStatus.ENDED and ended.ended_reason == "workspace_lost", "the open session was not ended"


# -- create_workspace (F4) ------------------------------------------------------------------------------
#
# The outside-prime refusal is gone (access-control P2 T6): a session outside prime creates a workspace like any other, and the creator's person
# is stamped owner of what they build.


async def _template_world() -> WsWorld:
    world = await _ws_world()
    await world.sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(access=Access(public=True), id="tpl", description="t", provider_id="local-1"))
    return world


@pytest.mark.parametrize("arguments", [
    pytest.param({"id": "ws-new", "template_id": "tpl"}, id="a new id"),
    pytest.param({"id": "ws-2", "template_id": "tpl"}, id="an id that exists"),
    pytest.param({"template_id": "no-such-template"}, id="an unknown template"),
])
@pytest.mark.parametrize(("own", "door"), [("ws-1", "tool manager"), ("ws-1", "system__call_tool"), ("ws-2", "system__call_tool")])
async def test_create_workspace_from_a_session_outside_prime_stamps_the_creator_owner(arguments, own, door):
    """F4, access version (P2 T6): a session outside prime creates a workspace like any other. A new id creates (and stamps the creator's person
    owner of it); an id that exists conflicts and an unknown template answers not-found, as from prime."""
    world = await _template_world()
    before = await world.snapshot()

    outcome, body = await asyncio.wait_for(_call(world, "workspaces", "create_workspace", arguments, own=own, door=door, run_as=ADA_REF), 30)

    if "ws-new" in arguments.get("id", ""):
        assert outcome == D, body
        row = await world.sp.get_storage(Workspace).get("ws-new")
        assert row is not None and row.access.owners == ["u-ada"], row.access
        assert "ws-new" in world.backend.live
    else:
        assert outcome == ("conflict" if arguments.get("id") else N), (arguments, body)
        assert await world.snapshot() == before


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


@pytest.mark.parametrize(("own", "initiator", "expected"), [
    pytest.param("ws-1", USER, N, id="a graph session outside prime, a person with no level"),
    pytest.param("ws-prime", USER, N, id="in prime, a person with no level"),
    pytest.param("ws-1", ADA_REF, D, id="a graph session outside prime, the owner"),
    pytest.param("ws-prime", ADA_REF, D, id="in prime, the owner"),
])
@pytest.mark.parametrize("door", DOORS)
async def test_the_tools_of_a_graph_session_with_no_holder_slot_act_as_the_run_s_person(own, initiator, expected, door, tmp_path):
    """F6, access version (P2 T6): the worker builds a holder-less graph session's per-node tool manager with no workspace session
    (``build_graph_executor``). The manager names the row's session and workspace on every context it builds, so the node's tools act as the
    run's person: a person with no level on ws-2 (u-user) is told it does not exist wherever the graph session sits, and its owner (u-ada)
    deletes it from either."""
    world = await _ws_world()
    # The run's person holds full on the graph session's own workspace, or the turn would not run at all (access-control P2: the worker
    # re-checks it at build); what is pinned here is the level the node's tools ask on ws-2.
    own_row = await world.sp.get_storage(Workspace).get(own)
    await world.sp.get_storage(Workspace).update(own_row.model_copy(update={
        "access": own_row.access.model_copy(update={"grants": {**own_row.access.grants, initiator.user_id: "full"}}),
    }))
    graph = _runnable_graph()
    row = WorkspaceSession(
        id="ses-g", workspace_id=own, binding=GraphSessionBinding(graph_id=graph.id, graph_snapshot=graph), status=SessionStatus.RUNNING,
        created_at=NOW, initiated_by=initiator,
    )
    await world.sp.get_storage(WorkspaceSession).create(row)
    for uid, name in (("u-user", "user"), ("u-ada", "ada")):  # the run's principal resolves through the User row
        await world.sp.get_storage(User).create(User(id=uid, username=name, created_at=NOW, role="user"))
    repo = LocalStateRepo(tmp_path / "state", workspace_id=own)
    await repo.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=world.sp, workspace_registry=world.workspaces,  # type: ignore[arg-type]
        provider_registry=world.registry, engine=InMemoryClaimEngine(adapters={}),  # type: ignore[arg-type]
    )
    driver = await pool._build_executor(row, _HolderlessHost(own, repo))  # noqa: SLF001 -- the worker's own build seam
    agent = Agent(access=Access(public=True), 
        id="planner", description="x", model=AgentModel(profile_id="p--m"), tools=["workspaces__delete_workspace", "system__call_tool"],
        system_prompt=[],
    )
    manager = await driver._executor._tool_manager_resolver(agent)  # noqa: SLF001 -- what every agent node of the run is given

    ctx = manager.context_for("call-1")
    assert ctx is not None and (ctx.session_id, ctx.workspace_id, ctx.initiated_by) == ("ses-g", own, initiator)
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


async def _trigger_world(target_workspace: str = "ws-2", owner: PrincipalRef = ADA_REF) -> WsWorld:
    """The rule-table world, a trigger ``tr-x`` (enabled, delayed) and one subscription of it, saved by ``owner`` (no recorded reach), starting a
    session in ``target_workspace``. The fire of the subscription is gated on ``owner``'s level on the target (P2 T6), so the caller passes the
    person who holds it."""
    world = await _ws_world()
    await world.sp.get_storage(Trigger).create(Trigger(
        id="tr-x", slug="tr-x", name="t", created_at=NOW, owner=USER, config=DelayedTriggerConfig(fire_at=NOW + timedelta(days=1)),
    ))
    await world.sp.get_storage(Subscription).create(Subscription(
        id="sb-x", trigger_id="tr-x", config=AgentFreshSubConfig(workspace_id=target_workspace, agent_id="planner"), created_at=NOW, owner=owner,
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
    world = await _trigger_world(target_workspace="ws-1", owner=USER)

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


# -- who may fire a trigger now (ticket 01a16e65-12d1) ---------------------------------------------------------------------------------------
#
# A manual fire (``POST /v1/triggers/{id}/fire_now`` and ``trigger__fire_now``, one service path since #915) drives the trigger owner's automation:
# the fire runs as its owners (``principal_for_fire``), so a ``session_append`` subscription of an admin's trigger steers at the admin's rank. It
# follows the rank rule (``primer.authz.may_drive``): the caller (the REST principal, the MCP client, or the calling session's run principal) must rank
# at least as high as the trigger's owner. An admin is never outranked; a person (over REST or MCP, never a run) may always fire a trigger they own;
# a token acts as its owner, for its rank and for what it owns. Otherwise 403 ``forbidden_rank`` (REST) or ``type=forbidden`` (a tool), and nothing
# fires: no ``trigger.fired`` event, no subscription dispatched, the trigger's ``last_fired_*`` as they were. A trigger saved before owners were
# recorded ranks as an ordinary user (``principal_rank(None)``). The reach rule (#874) is asked first, so an out-of-reach trigger still answers as an
# unknown one. The clock (a scheduled or delayed fire) has no caller and is not ranked.

FIRE_TRIGGER = "tr-r"
_FIRE_WORDS_REQUEST = (
    "trigger 'tr-r' runs at a higher rank ({owner}) than you ({caller}); you may fire only your own triggers and triggers at or below your rank"
)
_FIRE_WORDS_RUN = "trigger 'tr-r' runs at a higher rank ({owner}) than this session ({caller}); a session may fire only triggers at or below its own rank"
_FIRE_WORDS_RUN_OUTSIDE = "trigger 'tr-r' runs at a higher rank ({owner}) than this run ({caller}); a run may fire only triggers at or below its own rank"
ADA = Principal(type="user", id="u-ada", display="ada", role="user", source="local", user_id="u-ada")


async def _fire_events(sp) -> list:
    return await sp.get_event_store().read_after(0, event_type_prefix="trigger.fired")


async def _fire_rows(sp) -> tuple:
    """What a refused fire must leave as it was: the trigger row (its ``last_fired_*``), every subscription row, and the event log's fires."""
    return (
        (await sp.get_storage(Trigger).get(FIRE_TRIGGER)).model_dump(mode="json"),
        sorted(s.model_dump_json() for s in (await sp.get_storage(Subscription).list(OffsetPage(offset=0, length=100))).items),
        await _fire_events(sp),
    )


async def _fired(sp) -> bool:
    """Whether the fire happened: the trigger row records it and the event log has its ``trigger.fired``."""
    row = await sp.get_storage(Trigger).get(FIRE_TRIGGER)
    events = await _fire_events(sp)
    return row.last_fired_id is not None and row.last_fired_at is not None and [e.entity_id for e in events] == [FIRE_TRIGGER]


async def _rank_world(owner: PrincipalRef | None, *, user_full_on_ws2: bool = True) -> WsWorld:
    """The rule-table world, a trigger ``tr-r`` owned by ``owner`` with one subscription of it that starts a session in ws-2 (in reach of the calling
    session ``ses-o`` in ws-2), and an API token of ada's (``at-ada``) for the rows where a token acts as its owner. ws-2 grants full to the two
    ordinary accounts, so a fire the rank rule accepts is never held back by the subscription owner's level on the target (P2 T6)."""
    world = await _ws_world()
    grants = {"u-user": "full", "u-root": "full"} if user_full_on_ws2 else {"u-root": "full"}
    ws2 = (await world.sp.get_storage(Workspace).get("ws-2")).model_copy(update={"access": Access(owners=["u-ada"], grants=grants)})
    await world.sp.get_storage(Workspace).update(ws2)
    await world.sp.get_storage(Trigger).create(Trigger(
        id=FIRE_TRIGGER, slug=FIRE_TRIGGER, name="r", created_at=NOW, owner=owner, config=DelayedTriggerConfig(fire_at=NOW + timedelta(days=1)),
    ))
    await world.sp.get_storage(Subscription).create(Subscription(
        id="sb-r", trigger_id=FIRE_TRIGGER, config=AgentFreshSubConfig(workspace_id="ws-2", agent_id="planner"), created_at=NOW, owner=owner,
    ))
    await world.sp.get_storage(ApiToken).create(ApiToken(
        id="at-ada", user_id="u-ada", name="automation", token_hash="0" * 64, prefix="prmr_ada", created_at=NOW,
    ))
    return world


#: who -> (the trigger's owner, the caller: a request's actor over MCP, or the principal of a run in ws-2; whether it may fire). ``ADA_THEN`` is ada
#: recorded while she was an admin; she is a user now.
FIRE_CALLERS: dict[str, tuple[PrincipalRef | None, Principal | PrincipalRef, bool]] = {
    "MCP: a user on an admin's trigger": (ADMIN, MCP_USER, False),
    "MCP: an admin on a user's trigger": (USER, Principal(type="user", id="u-root", display="root", role="admin", source="local", user_id="u-root"), True),
    "MCP: a user on their own trigger": (USER, Principal(type="user", id="u-user", display="user", role="user", source="local", user_id="u-user"), True),
    "MCP: a demoted admin on their own trigger": (ADA_THEN, ADA, True),
    "MCP: a user's token on an admin's trigger": (
        ADMIN, Principal(type="api_token", id="at-user", display="automation", role="user", source="internal", user_id="u-user"), False,
    ),
    "MCP: an admin's token on a user's trigger": (
        USER, Principal(type="api_token", id="at-root", display="automation", role="admin", source="internal", user_id="u-root"), True,
    ),
    "MCP: a demoted admin's token on their own trigger": (
        ADA_THEN, Principal(type="api_token", id="at-ada", display="automation", role="user", source="internal", user_id="u-ada"), True,
    ),
    "MCP: a user on a trigger saved before owners were recorded": (None, MCP_USER, True),
    "MCP: a user on a system trigger": (PrincipalRef.system(), MCP_USER, False),
    "a user's run on an admin's trigger": (ADMIN, USER, False),
    "an admin's run on a user's trigger": (USER, ADMIN, True),
    "a user's run on their own trigger": (USER, USER, True),
    "a run with its owner's identity at a lower rank (a trigger-capped run)": (ADA_THEN, ADA_THEN.model_copy(update={"role": "user"}), False),
    "a user's run on a trigger saved before owners were recorded": (None, USER, True),
    "a user's run on a system trigger": (PrincipalRef.system(), USER, False),
    "an admin's run on a system trigger": (PrincipalRef.system(), ADMIN, True),
}
_FIRE_ROWS = [
    (who, door) for who in FIRE_CALLERS for door in (("MCP",) if who.startswith("MCP: ") else DOORS)
]


@pytest.mark.parametrize(("who", "door"), _FIRE_ROWS)
async def test_fire_now_fires_a_trigger_only_when_the_caller_ranks_with_its_owner(who, door):
    owner, caller, accepted = FIRE_CALLERS[who]
    world = await _rank_world(owner)
    before = await _fire_rows(world.sp)

    if isinstance(caller, Principal):
        outcome, body = await asyncio.wait_for(
            _call(world, "trigger", "fire_now", {"id": FIRE_TRIGGER}, own=None, door=door, actor=caller), 30,
        )
    else:
        outcome, body = await asyncio.wait_for(
            _call(world, "trigger", "fire_now", {"id": FIRE_TRIGGER}, own="ws-2", door=door, run_as=caller), 30,
        )

    if not accepted:
        words = (_FIRE_WORDS_REQUEST if isinstance(caller, Principal) else _FIRE_WORDS_RUN).format(
            owner="system" if owner.type == "system" else owner.role, caller="user",
        )
        assert (outcome, body) == (F, {"type": F, "message": words}), (who, door, body)
        assert await _fire_rows(world.sp) == before, f"a refused fire fired ({who}, {door})"
        return
    assert outcome == D, (who, door, body)
    assert await _fired(world.sp), (who, door, body)
    assert [r["subscription_id"] for r in body["results"]] == ["sb-r"], body


@pytest.mark.parametrize("door", DOORS)
async def test_fire_now_on_an_out_of_reach_trigger_answers_as_an_unknown_one_before_any_rank(door):
    """The access rule is asked first: an admin's trigger whose subscription starts a session in ws-2, where the user holds no full, is unknown, not
    forbidden, for a user's run, and nothing fires."""
    world = await _rank_world(ADMIN, user_full_on_ws2=False)
    before = await _fire_rows(world.sp)

    outcome, body = await asyncio.wait_for(_call(world, "trigger", "fire_now", {"id": FIRE_TRIGGER}, own="ws-1", door=door, run_as=USER), 30)

    assert (outcome, body) == ("trigger_not_found", {"type": "trigger_not_found", "message": FIRE_TRIGGER}), body
    assert await _fire_rows(world.sp) == before
    _, unknown = await _call(world, "trigger", "fire_now", {"id": "tr-nope"}, own="ws-1", door=door, run_as=USER)
    assert json.dumps(body).replace(FIRE_TRIGGER, "<id>") == json.dumps(unknown).replace("tr-nope", "<id>"), (body, unknown)


@pytest.mark.parametrize("door", DOORS)
async def test_an_admins_run_fires_a_trigger_whatever_workspaces_it_targets(door):
    """An admin clears the access rule (it holds every level), so the same trigger an unprivileged user cannot see fires for an admin's run."""
    world = await _rank_world(ADMIN, user_full_on_ws2=False)

    outcome, body = await asyncio.wait_for(_call(world, "trigger", "fire_now", {"id": FIRE_TRIGGER}, own="ws-1", door=door, run_as=ADMIN), 30)

    assert outcome == D, body
    assert await _fired(world.sp), body


async def test_the_clock_fires_an_admins_trigger_with_no_caller_to_rank():
    """Only the manual fire doors are ranked: the worker's trigger handler (a scheduled or delayed fire coming due) fires an admin's trigger."""
    world = await _fire_world()
    trigger = await world.sp.get_storage(Trigger).get("tr-fire")
    await world.sp.get_storage(Trigger).update(trigger.model_copy(update={"owner": ADMIN}))
    pool = SimpleNamespace(
        _storage=world.sp, _engine=world.engine, _scheduler=world.scheduler, _workspace_registry=world.registry, _event_bus=world.bus,
        _release_lease=lambda lease, outcome: asyncio.sleep(0),
    )

    await asyncio.wait_for(run_engine_trigger(pool, SimpleNamespace(entity_id="tr-fire")), 30)  # type: ignore[arg-type]

    assert (await world.sp.get_storage(Trigger).get("tr-fire")).last_fired_id is not None


def test_the_fire_now_descriptor_says_who_may_fire_and_what_a_refusal_answers():
    from primer.toolset.trigger import TOOL_FIRE_NOW

    text = TOOL_FIRE_NOW.description
    assert "rank at least as high as its owner" in text and "``type=forbidden`` and nothing fires" in text, text
    assert "you may also fire your own" in text and "answers ``trigger_not_found``, not ``forbidden``" in text, text


#: who -> (the trigger's owner, who asks over REST, whether it may fire). ``carol`` ranks user, ``admin`` is the app's first account; ``carol then
#: admin`` is carol recorded while she was an admin (she is a user now); a token row asks with an API token of carol or of the admin.
REST_FIRERS: dict[str, tuple[str | None, str, bool]] = {
    "a user on an admin's trigger": ("admin", "carol", False),
    "an admin on a user's trigger": ("carol", "admin", True),
    "a user on their own trigger": ("carol", "carol", True),
    "a demoted admin on their own trigger": ("carol then admin", "carol", True),
    "a user's token on an admin's trigger": ("admin", "carol's token", False),
    "an admin's token on a user's trigger": ("carol", "admin's token", True),
    "a demoted admin's token on their own trigger": ("carol then admin", "carol's token", True),
    "a demoted admin on a trigger their token saved": ("carol's token then admin", "carol", True),
    "a user on a trigger saved before owners were recorded": (None, "carol", True),
    "a user on a system trigger": ("system", "carol", False),
    "an admin on a system trigger": ("system", "admin", True),
}


@pytest.mark.parametrize("who", list(REST_FIRERS))
async def test_the_rest_fire_now_fires_a_trigger_only_when_the_caller_ranks_with_its_owner(app, client, fake_storage_provider, who):
    """Over HTTP, through the auth middleware. The subscription appends to a busy session started by the trigger's owner, so a delivered fire queues
    a message there; a refused one queues nothing, publishes nothing and records nothing."""
    owner_name, asker, accepted = REST_FIRERS[who]
    sp = fake_storage_provider
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    carol_id, admin_id = await _user_id(app, "carol"), await _user_id(app, "testuser")
    carol_token = await _token_client(app, carol_id)  # creates the token row ``at-<carol>`` (the owner of one row below)
    owner = {
        None: None,
        "system": PrincipalRef.system(),
        "admin": PrincipalRef(type="user", id=admin_id, display="testuser", role="admin", source="local"),
        "carol": PrincipalRef(type="user", id=carol_id, display="carol", role="user", source="local"),
        "carol then admin": PrincipalRef(type="user", id=carol_id, display="carol", role="admin", source="local"),
        "carol's token then admin": PrincipalRef(type="api_token", id=f"at-{carol_id}", display="automation", role="admin", source="internal"),
    }[owner_name]
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="ses-busy", workspace_id="ws-r", binding=AgentSessionBinding(agent_id="operator"), status=SessionStatus.RUNNING, turn_status="running",
        turn_no=1, created_at=NOW, initiated_by=owner,
    ))
    await sp.get_storage(Trigger).create(Trigger(
        id=FIRE_TRIGGER, slug=FIRE_TRIGGER, name="r", created_at=NOW, owner=owner, config=DelayedTriggerConfig(fire_at=NOW + timedelta(days=1)),
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-r", trigger_id=FIRE_TRIGGER, config=SessionAppendSubConfig(session_id="ses-busy"), payload_template="fired", parallelism="queue",
        created_at=NOW, owner=owner,
    ))
    before = await _fire_rows(sp)
    session_before = (await sp.get_storage(WorkspaceSession).get("ses-busy")).model_dump(mode="json")
    spy = _Spy(app.state.event_bus)
    route = f"/v1/triggers/{FIRE_TRIGGER}/fire_now"

    async with carol_token:
        if asker == "carol's token":
            r = await carol_token.post(route)
        elif asker == "admin's token":
            async with await _token_client(app, admin_id) as tok:
                r = await tok.post(route)
        elif asker == "carol":
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
                assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
                r = await carol.post(route)
        else:
            r = await client.post(route)

    if not accepted:
        assert r.status_code == 403, f"{who} could fire: {r.text}"
        words = _FIRE_WORDS_REQUEST.format(owner="system" if owner.type == "system" else owner.role, caller="user")
        assert (r.json()["detail"], r.json()["extensions"]["code"]) == (words, "forbidden_rank"), r.json()
        assert await _fire_rows(sp) == before, f"a refused fire fired ({who})"
        assert (await sp.get_storage(WorkspaceSession).get("ses-busy")).model_dump(mode="json") == session_before
        assert (await sp.get_storage(PendingSessionMessage).list(OffsetPage(offset=0, length=10))).items == []
        assert spy.keys == [], f"a refused fire reached the event bus ({who})"
        return
    assert r.status_code == 200, f"{who} could not fire: {r.text}"
    assert await _fired(sp), r.text
    assert [res["subscription_id"] for res in r.json()["results"]] == ["sb-r"], r.json()


# -- round 2 of #927: the real MCP endpoint, the approval resume, a delegated run, and the owner read by the fire ---------------------------------

#: who -> (the trigger's owner, who asks over the REAL ``POST /v1/mcp`` (a cookie, or a token with the ``mcp`` scope), whether it may fire).
MCP_FIRERS: dict[str, tuple[str | None, str, bool]] = {
    "a user's cookie on an admin's trigger": ("admin", "carol", False),
    "a user's mcp token on an admin's trigger": ("admin", "carol's token", False),
    "a user's cookie on a system trigger": ("system", "carol", False),
    "an admin's cookie on an admin's trigger": ("admin", "admin", True),
    "an admin's cookie on a system trigger": ("system", "admin", True),
    "an admin's mcp token on a user's trigger": ("carol", "admin's token", True),
    "a demoted admin's cookie on their own trigger": ("carol then admin", "carol", True),
    "a demoted admin's mcp token on their own trigger": ("carol then admin", "carol's token", True),
    "a user's cookie on a trigger saved before owners were recorded": (None, "carol", True),
}


async def _cookie_of(app, username: str, password: str) -> str:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/v1/auth/login", json={"username": username, "password": password})).status_code == 200
        return "; ".join(f"{k.name}={k.value}" for k in c.cookies.jar)


async def _mcp_token(sp, user_id: str) -> str:
    from primer.auth.api_tokens import extract_prefix, hash_token, mint_plaintext

    plaintext = mint_plaintext()
    await sp.get_storage(ApiToken).create(ApiToken(
        id=f"at-mcp-{user_id}", user_id=user_id, name="mcp", token_hash=hash_token(plaintext), prefix=extract_prefix(plaintext), scopes=["mcp"],
        created_at=NOW,
    ))
    return plaintext


@pytest.mark.parametrize("who", list(MCP_FIRERS))
async def test_fire_now_over_the_real_mcp_endpoint_fires_a_trigger_only_when_the_caller_ranks_with_its_owner(app, client, fake_storage_provider, who):
    """Through ``POST /v1/mcp`` (the auth gate, ``invoke_exposed``, ``current_actor``): the person's own call, with the owner rule."""
    from tests.api.test_mcp_mount import _mcp_call

    owner_name, asker, accepted = MCP_FIRERS[who]
    sp = fake_storage_provider
    assert (await client.put("/v1/mcp_exposure", json={"enabled": True, "allowed_tools": ["trigger__fire_now"]})).status_code == 200
    assert (await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})).status_code == 201
    carol_id, admin_id = await _user_id(app, "carol"), await _user_id(app, "testuser")
    owner = {
        None: None,
        "system": PrincipalRef.system(),
        "admin": PrincipalRef(type="user", id=admin_id, display="testuser", role="admin", source="local"),
        "carol": PrincipalRef(type="user", id=carol_id, display="carol", role="user", source="local"),
        "carol then admin": PrincipalRef(type="user", id=carol_id, display="carol", role="admin", source="local"),
    }[owner_name]
    # The workspace the subscription starts a session in: carol holds full on it (the access rule asks the caller's person; an admin clears it).
    await sp.get_storage(Workspace).create(_ws_row("ws-x", Access(owners=[carol_id])))
    await sp.get_storage(Trigger).create(Trigger(
        id=FIRE_TRIGGER, slug=FIRE_TRIGGER, name="r", created_at=NOW, owner=owner, config=DelayedTriggerConfig(fire_at=NOW + timedelta(days=1)),
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-r", trigger_id=FIRE_TRIGGER, config=AgentFreshSubConfig(workspace_id="ws-x", agent_id="planner"), created_at=NOW, owner=owner,
    ))
    before = await _fire_rows(sp)
    headers = {
        "carol": lambda: _cookie_of(app, "carol", "carolpw1"),
        "admin": lambda: _cookie_of(app, "testuser", "testpassword"),
    }
    if asker in headers:
        sent = {"Cookie": await headers[asker]()}
    else:
        sent = {"Authorization": f"Bearer {await _mcp_token(sp, carol_id if asker.startswith('carol') else admin_id)}"}

    result = await _mcp_call(app, headers=sent, tool_name="trigger__fire_now", arguments={"id": FIRE_TRIGGER})

    text = result.content[0].text
    if not accepted:
        words = _FIRE_WORDS_REQUEST.format(owner="system" if owner.type == "system" else owner.role, caller="user")
        assert (result.is_error, json.loads(text)) == (True, {"type": F, "message": words}), text
        assert await _fire_rows(sp) == before, f"a refused MCP fire fired ({who})"
        return
    assert result.is_error is False, text
    assert await _fired(sp), text


@pytest.mark.parametrize(("run_as", "accepted"), [pytest.param(USER, False, id="a user's run"), pytest.param(ADMIN, True, id="an admin's run")])
async def test_an_approved_fire_now_through_call_tool_ranks_the_resumed_run(run_as, accepted):
    """The approval resume of a gated ``system__call_tool`` -> ``trigger::fire_now`` (``resume_call_tool_dispatch``) runs the call as the RESUMED
    run, so it is ranked as that run: a user's run is refused in a session's words and nothing fires; an admin's run fires."""
    from primer.worker.yield_runtime import resume_call_tool_dispatch

    world = await _rank_world(ADMIN)
    before = await _fire_rows(world.sp)
    manager = ToolExecutionManager(
        toolset_providers=world.providers, provider_registry=world.registry,
        workspace_session=SimpleNamespace(session_id=CALLERS["ws-2"], workspace_id="ws-2", agent_id="planner"),  # type: ignore[arg-type]
        initiated_by=run_as,
    )

    part = await asyncio.wait_for(resume_call_tool_dispatch(
        via={"toolset_id": "trigger"}, original_call=ToolCallPart(id="c-res", name="fire_now", arguments={"id": FIRE_TRIGGER}),
        tool_manager=manager,
    ), 30)

    if not accepted:
        assert (part.error, json.loads(part.output)) == (True, {"type": F, "message": _FIRE_WORDS_RUN.format(owner="admin", caller="user")})
        assert await _fire_rows(world.sp) == before
        return
    assert not part.error, part.output
    assert await _fired(world.sp)


class _FireOnceLLM:
    """The subagent's model: its first answer calls ``trigger__fire_now``, its second ends the run. It keeps what it was sent."""

    def __init__(self) -> None:
        self.sent: list = []

    def stream(self, *, model, messages, **kwargs):  # noqa: ANN001
        from primer.model.chat import Done, StreamStart, TextDelta, ToolCallEnd, ToolCallStart

        self.sent.append(list(messages))
        if len(self.sent) == 1:
            script = [
                StreamStart(model="m1"), ToolCallStart(id="call-f", name="trigger__fire_now", index=0),
                ToolCallEnd(id="call-f", arguments={"id": FIRE_TRIGGER}, index=0), Done(stop_reason="tool_use", raw_reason="tool_use"),
            ]
        else:
            script = [StreamStart(model="m1"), TextDelta(index=0, text="done"), Done(stop_reason="stop", raw_reason="stop")]

        async def _gen():
            for event in script:
                yield event

        return _gen()


def _tool_result_of(llm: _FireOnceLLM) -> dict:
    from primer.model.chat import ToolResultPart

    [result] = [p for m in llm.sent[-1] for p in m.parts if isinstance(p, ToolResultPart)]
    return json.loads(result.output)


@pytest.mark.parametrize("actor", [
    pytest.param(Principal(type="user", id="u-root", display="root", role="admin", source="local"), id="under an admin's MCP request"),
    pytest.param(MCP_USER, id="under a user's MCP request"),
])
async def test_a_subagent_started_over_mcp_is_ranked_as_its_run_not_as_the_mcp_person(actor):
    """Lead ruling on the review of #927: ``system__invoke_agent`` over MCP (no tool context) runs its subagent as an unattributed user
    (``PrincipalRef.unattributed()``), and the subagent's tool calls carry no context either. Inside that delegated run (``_RUN_ID`` set) the
    fire is ranked by the run's principal with no owner rule, never as the MCP person whose actor is still on ``current_actor``: the subagent is
    refused an admin's trigger even under an admin's request, in a run's words, and nothing fires. Driven through the real ``run_subagent``."""
    from primer.agent.invoke import invocation_depth_guard, run_subagent
    from tests.agent.test_run_subagent_yield import _agent, _provider_row, _ProviderRegistry, _StorageProvider

    world = await _rank_world(ADMIN)
    before = await _fire_rows(world.sp)
    llm = _FireOnceLLM()
    registry = _ProviderRegistry(llm=llm, toolset=world.trigger)  # type: ignore[arg-type]
    token = current_actor.set(actor)
    try:
        with invocation_depth_guard():
            text = await asyncio.wait_for(run_subagent(
                agent_id="agent-sub", prompt="fire it", storage_provider=_StorageProvider(agent=_agent(tools=["trigger__fire_now"]),
                                                                                           provider_row=_provider_row()),
                provider_registry=registry, identity=PrincipalRef.unattributed(),  # what invoke_agent passes when it has no context
            ), 30)
    finally:
        current_actor.reset(token)

    assert text == "done"
    assert _tool_result_of(llm) == {"type": F, "message": _FIRE_WORDS_RUN_OUTSIDE.format(owner="admin", caller="user")}
    assert await _fire_rows(world.sp) == before, "a subagent fired an admin's trigger at the MCP person's rank"


def test_a_call_nobody_can_name_is_worded_as_an_ordinary_user():
    """A call with no context, no actor and no delegated run ranks as an ordinary user (``principal_rank(None)``) and is worded as one."""
    from primer.trigger.owner import refuse_fire_now

    trigger = Trigger(id=FIRE_TRIGGER, slug=FIRE_TRIGGER, name="r", created_at=NOW, owner=ADMIN, config=DelayedTriggerConfig(fire_at=NOW))

    reason = asyncio.run(refuse_fire_now(trigger, None, _FakeStorageProvider(), from_run=False))

    assert reason == _FIRE_WORDS_REQUEST.format(owner="admin", caller="user")
    assert "unattributed" not in reason


#: Who saves the trigger between the rank check and ``fire_trigger``'s read -> whether the fire goes ahead (the caller ranks user).
SAVED_BY = {
    "an admin (the saver becomes the owner, above the caller)": (ADMIN, False),
    "another user (an owner the caller may fire anyway)": (PrincipalRef(type="user", id="u-other", display="other", role="user", source="local"), True),
}


@pytest.mark.parametrize("door", ["a user's run", "MCP: a user"])
@pytest.mark.parametrize("saved_by", list(SAVED_BY))
async def test_an_owner_saved_between_the_rank_check_and_the_fire_is_ranked_again_on_the_row_that_is_fired(monkeypatch, saved_by, door):
    """Review of #927: the rank was decided on ``fire_now``'s read and ``fire_trigger`` read the row again, so a save in between (which makes its
    saver the owner) fired the trigger at the saver's rank. The checked owner now goes to ``fire_trigger``: when its read shows another owner the
    rule is asked again on that row, and a refusal there is the same refusal, before anything is recorded or dispatched. A save by an owner the
    caller may fire anyway does not stop the fire (refusing it would say the trigger runs above the caller when it does not)."""
    import primer.trigger.dispatch as dispatch_mod

    new_owner, fires = SAVED_BY[saved_by]
    world = await _rank_world(USER)
    real_fire = dispatch_mod.fire_trigger

    async def save_then_fire(**kwargs):
        row = await world.sp.get_storage(Trigger).get(kwargs["trigger_id"])
        await world.sp.get_storage(Trigger).update(row.model_copy(update={"owner": new_owner}))
        return await real_fire(**kwargs)

    monkeypatch.setattr(dispatch_mod, "fire_trigger", save_then_fire)
    if door == "a user's run":
        outcome, body = await asyncio.wait_for(_call(world, "trigger", "fire_now", {"id": FIRE_TRIGGER}, own="ws-2", door="tool manager"), 30)
    else:
        outcome, body = await asyncio.wait_for(_call(world, "trigger", "fire_now", {"id": FIRE_TRIGGER}, own=None, door="MCP"), 30)

    row = await world.sp.get_storage(Trigger).get(FIRE_TRIGGER)
    assert row.owner == new_owner, "the save landed"
    if fires:
        assert outcome == D, body
        assert await _fired(world.sp)
        return
    words = (_FIRE_WORDS_RUN if door == "a user's run" else _FIRE_WORDS_REQUEST).format(owner="admin", caller="user")
    assert (outcome, body) == (F, {"type": F, "message": words}), body
    assert (row.last_fired_id, row.last_fired_at) == (None, None), "the trigger records a fire"
    assert await _fire_events(world.sp) == [], "a trigger.fired was recorded"
    assert all(s.last_fired_at is None for s in (await _subscriptions(world)).values()), "a subscription was dispatched"


# -- who may change or delete a trigger (ticket 01a16ee4) --------------------------------------------------------------------------------------
#
# A save re-arms the trigger's claim under the saver's timing, and a delete ends it: both act on every subscription the trigger drives, so
# ``update_trigger`` and ``delete_trigger`` follow the rank rule of a manual fire (``primer.trigger.owner.refuse_trigger_change``, the same rule
# as ``refuse_fire_now``): the caller (the REST principal, the MCP person, or the calling run's principal) must rank at least as high as the
# trigger's owner. An admin is never outranked; a person (over REST or MCP, never a run) may always change or delete a trigger they own.
# Otherwise 403 ``forbidden_rank`` (REST) or ``type=forbidden`` (a tool), and nothing changes: the row, its owner and its subscriptions stay as
# they were, and a refused change adopts no owner (the caller does not become the owner of a trigger they may not save). The reach rule (#874)
# is asked first, as for a fire. A tool call with no context, the MCP endpoint's own, records the MCP principal as the owner of what it saves.

_CHANGE_WORDS_REQUEST = (
    "trigger 'tr-r' runs at a higher rank ({owner}) than you ({caller}); you may change or delete only your own triggers and triggers at or below your rank"
)
_CHANGE_WORDS_RUN = "trigger 'tr-r' runs at a higher rank ({owner}) than this session ({caller}); a session may change or delete only triggers at or below its own rank"


def _change_args(verb: str) -> dict:
    return {"id": FIRE_TRIGGER, "name": "edited"} if verb == "update" else {"id": FIRE_TRIGGER}


async def _change_rows(sp) -> tuple:
    """What a refused change or delete must leave as it was: the trigger row (its owner, its timing) and every subscription row."""
    row = await sp.get_storage(Trigger).get(FIRE_TRIGGER)
    subs = (await sp.get_storage(Subscription).list(OffsetPage(offset=0, length=100))).items
    return (None if row is None else row.model_dump(mode="json"), sorted(s.model_dump_json() for s in subs))


@pytest.mark.parametrize(
    ("who", "door", "verb"),
    [(who, door, verb) for who in FIRE_CALLERS for door in (("MCP",) if who.startswith("MCP: ") else DOORS) for verb in ("update", "delete")],
)
async def test_update_and_delete_touch_a_trigger_only_when_the_caller_ranks_with_its_owner(who, door, verb):
    """The same rule as a manual fire: the rows of ``FIRE_CALLERS`` on the ``trigger::update`` / ``trigger::delete`` doors (and the crud twins
    that share their handlers). A refused change or delete leaves the row, its owner and its subscriptions exactly as they were; an accepted
    update makes its caller the owner."""
    owner, caller, accepted = FIRE_CALLERS[who]
    world = await _rank_world(owner)
    before = await _change_rows(world.sp)

    if isinstance(caller, Principal):
        outcome, body = await asyncio.wait_for(
            _call(world, "trigger", verb, _change_args(verb), own=None, door=door, actor=caller), 30,
        )
    else:
        outcome, body = await asyncio.wait_for(
            _call(world, "trigger", verb, _change_args(verb), own="ws-2", door=door, run_as=caller), 30,
        )

    if not accepted:
        words = (_CHANGE_WORDS_REQUEST if isinstance(caller, Principal) else _CHANGE_WORDS_RUN).format(
            owner="system" if owner.type == "system" else owner.role, caller="user",
        )
        assert (outcome, body) == (F, {"type": F, "message": words}), (who, door, verb, body)
        assert await _change_rows(world.sp) == before, f"a refused {verb} changed the trigger ({who}, {door})"
        return
    assert outcome == D, (who, door, verb, body)
    row = await world.sp.get_storage(Trigger).get(FIRE_TRIGGER)
    if verb == "delete":
        assert row is None, body
        assert (await world.sp.get_storage(Subscription).list(OffsetPage(offset=0, length=100))).items == [], body
    else:
        assert row.name == "edited", body
        assert row.owner is not None and row.owner.id == caller.id, (who, door, row.owner)


async def test_a_create_over_mcp_records_the_mcp_principal_as_owner():
    """Ticket 01a16ee4: a tool call with no context (the MCP endpoint's own) used to record no owner, so what it saved fired as an ordinary user
    whatever the person's rank. It records the MCP principal: a row it saves fires at most at the person's rank, and a later save by a lower
    rank is refused by the rank rule."""
    world = await _ws_world()

    _outcome, _body = await asyncio.wait_for(_call(
        world, "trigger", "create",
        {"slug": "mcp-made", "name": "made", "config": {"kind": "delayed", "fire_at": (NOW + timedelta(days=1)).isoformat()}},
        own=None, door="MCP", actor=MCP_USER,
    ), 30)

    rows = (await world.sp.get_storage(Trigger).list(OffsetPage(offset=0, length=10))).items
    [row] = [t for t in rows if t.slug == "mcp-made"]
    assert (row.owner.type, row.owner.id, row.owner.role) == ("user", MCP_USER.id, "user"), row.owner


@pytest.mark.parametrize(
    "actor",
    [
        pytest.param(MCP_USER, id="a user's MCP request"),
        pytest.param(Principal(type="user", id="u-root", display="root", role="admin", source="local"), id="an admin's MCP request"),
    ],
)
async def test_an_update_over_mcp_records_the_mcp_principal_as_owner(actor):
    world = await _rank_world(None)  # the trigger has no recorded owner: any rank may save it, and the saver becomes the owner

    _outcome, _body = await asyncio.wait_for(
        _call(world, "trigger", "update", {"id": FIRE_TRIGGER, "name": "edited"}, own=None, door="MCP", actor=actor), 30,
    )

    row = await world.sp.get_storage(Trigger).get(FIRE_TRIGGER)
    assert row.name == "edited"
    assert (row.owner.type, row.owner.id, row.owner.role) == ("user", actor.id, actor.role), row.owner


@pytest.mark.parametrize(("who", "verb"), [(who, verb) for who in REST_FIRERS for verb in ("update", "delete")])
async def test_the_rest_update_and_delete_touch_a_trigger_only_when_the_caller_ranks_with_its_owner(app, client, fake_storage_provider, who, verb):
    """Over HTTP, through the auth middleware: ``PUT /v1/triggers/{id}`` and ``DELETE /v1/triggers/{id}``, the rows of ``REST_FIRERS``. A
    refused change or delete is a 403 ``forbidden_rank`` and leaves the row, its owner and its subscriptions exactly as they were; an accepted
    update makes its asker the owner."""
    owner_name, asker, accepted = REST_FIRERS[who]
    sp = fake_storage_provider
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    carol_id, admin_id = await _user_id(app, "carol"), await _user_id(app, "testuser")
    carol_token = await _token_client(app, carol_id)  # creates the token row ``at-<carol>`` (the owner of one row below)
    owner = {
        None: None,
        "system": PrincipalRef.system(),
        "admin": PrincipalRef(type="user", id=admin_id, display="testuser", role="admin", source="local"),
        "carol": PrincipalRef(type="user", id=carol_id, display="carol", role="user", source="local"),
        "carol then admin": PrincipalRef(type="user", id=carol_id, display="carol", role="admin", source="local"),
        "carol's token then admin": PrincipalRef(type="api_token", id=f"at-{carol_id}", display="automation", role="admin", source="internal"),
    }[owner_name]
    await sp.get_storage(Trigger).create(Trigger(
        id=FIRE_TRIGGER, slug=FIRE_TRIGGER, name="r", created_at=NOW, owner=owner, config=DelayedTriggerConfig(fire_at=NOW + timedelta(days=1)),
    ))
    await sp.get_storage(Subscription).create(Subscription(
        id="sb-r", trigger_id=FIRE_TRIGGER, config=AgentFreshSubConfig(workspace_id="ws-x", agent_id="planner"), created_at=NOW, owner=owner,
    ))
    before = await _change_rows(sp)
    route = f"/v1/triggers/{FIRE_TRIGGER}"

    async def _ask():
        if asker == "carol's token":
            async with carol_token:
                return await carol_token.put(route, json={"name": "edited"}) if verb == "update" else await carol_token.delete(route)
        if asker == "admin's token":
            async with await _token_client(app, admin_id) as tok:
                return await tok.put(route, json={"name": "edited"}) if verb == "update" else await tok.delete(route)
        if asker == "carol":
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
                assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
                return await carol.put(route, json={"name": "edited"}) if verb == "update" else await carol.delete(route)
        return await client.put(route, json={"name": "edited"}) if verb == "update" else await client.delete(route)

    r = await _ask()

    if not accepted:
        words = _CHANGE_WORDS_REQUEST.format(owner="system" if owner.type == "system" else owner.role, caller="user")
        assert r.status_code == 403, f"{who} could {verb}: {r.text}"
        assert (r.json()["detail"], r.json()["extensions"]["code"]) == (words, "forbidden_rank"), r.json()
        assert await _change_rows(sp) == before, f"a refused {verb} changed the trigger ({who})"
        return
    assert r.status_code == (200 if verb == "update" else 204), f"{who} could not {verb}: {r.text}"
    row = await sp.get_storage(Trigger).get(FIRE_TRIGGER)
    if verb == "delete":
        assert row is None
        assert (await sp.get_storage(Subscription).list(OffsetPage(offset=0, length=100))).items == []
    else:
        assert r.json()["name"] == "edited"
        expected_owner_id = {"carol": carol_id, "admin": admin_id, "carol's token": f"at-{carol_id}", "admin's token": f"at-{admin_id}"}[asker]
        assert row.owner is not None and row.owner.id == expected_owner_id, (who, row.owner)


def test_the_update_and_delete_descriptors_say_the_rank_rule_and_what_a_refusal_answers():
    from primer.toolset.trigger import TOOL_CREATE, TOOL_DELETE, TOOL_UPDATE

    for tool in (TOOL_UPDATE, TOOL_DELETE):
        flat = " ".join(tool.description.split())
        assert "rank at least as high as its owner" in flat, tool.id
        assert "``type=forbidden``" in flat, tool.id
    flat = " ".join(TOOL_CREATE.description.split())
    assert "records you as the trigger's ``owner``" in flat, flat


# -- the subscription reads and the single-subscription deletes (ticket 01a16c22) -------------------------------------------------------------
#
# A subscription is in the workspace of its target: the workspace a fresh session starts in, or the workspace of the session it steers or wakes.
# From a session outside prime the tools that read subscriptions serve only those whose target is in reach, and the tools that delete one answer an
# out-of-reach one exactly as an unknown subscription id and delete nothing. A target is classified as the save check classifies it
# (``refuse_target_out_of_reach``): a target session that no longer exists is in no workspace, so out of reach. From prime, with no session (MCP) and
# over REST (a person) nothing changes.

#: door -> (toolset, tool, the trigger it is asked on): the tools that list a trigger's subscriptions.
LIST_DOORS: dict[str, tuple[str, str, str]] = {
    "trigger::list_subscriptions": ("trigger", "list_subscriptions", TRIGGER),
    "system::list_channel_bindings": ("system", "list_channel_bindings", CHANNEL_TRIGGER),
}
#: door -> (toolset, tool, the trigger it is asked on): the tools that name one subscription.
ONE_DOORS: dict[str, tuple[str, str, str]] = {
    "trigger::get_subscription": ("trigger", "get_subscription", TRIGGER),
    "trigger::delete_subscription": ("trigger", "delete_subscription", TRIGGER),
    "system::delete_channel_binding": ("system", "delete_channel_binding", CHANNEL_TRIGGER),
}
#: name -> the config of a subscription seeded on each trigger, saved by a person (no recorded reach). ``ses-a`` is in ws-1, ``ses-t`` in ws-2.
TARGETS: dict[str, object] = {
    "fresh-ws1": AgentFreshSubConfig(workspace_id="ws-1", agent_id="planner"),
    "append-ws1": SessionAppendSubConfig(session_id="ses-a"),
    "fresh-ws2": AgentFreshSubConfig(workspace_id="ws-2", agent_id="planner"),
    "graph-ws2": GraphFreshSubConfig(workspace_id="ws-2", graph_id="g-1"),
    "append-ws2": SessionAppendSubConfig(session_id="ses-t"),
    "parked-ws2": ParkedSessionSubConfig(session_id="ses-t", tool_call_id="tc-1", parked_at=NOW),
    "fresh-prime": AgentFreshSubConfig(workspace_id="ws-prime", agent_id="planner"),
    "append-gone": SessionAppendSubConfig(session_id="ses-gone"),
}
#: the calling session's workspace (None: no calling session) -> the subscriptions its person holds full on the target of (access-control P2 T6):
#: u-user holds ws-1 only; u-ada (ws-2, ws-prime and the MCP call) holds ws-2 and ws-prime. A target session that is gone is in no workspace: none see it.
_ADA_SEES = {"fresh-ws2", "graph-ws2", "append-ws2", "parked-ws2", "fresh-prime"}
VISIBLE: dict[str | None, set[str]] = {
    "ws-1": {"fresh-ws1", "append-ws1"},
    "ws-2": _ADA_SEES,
    "ws-prime": _ADA_SEES,
    None: _ADA_SEES,
}


def _sid(trigger_id: str, name: str) -> str:
    return f"{trigger_id}.{name}"


async def _subscription_world() -> WsWorld:
    """The rule-table world, its two triggers, and every subscription of ``TARGETS`` on each."""
    world = await _seed_world_for_subscriptions()
    store = world.sp.get_storage(Subscription)
    for trigger_id in (TRIGGER, CHANNEL_TRIGGER):
        for name, config in TARGETS.items():
            await store.create(Subscription(id=_sid(trigger_id, name), trigger_id=trigger_id, config=config, created_at=NOW, owner=USER))
    return world


@pytest.mark.parametrize(("door_name", "who", "door"), _rows(LIST_DOORS))
async def test_the_subscription_lists_serve_only_what_the_caller_reaches(door_name, who, door):
    """Ruling 1: from ws-1 a ws-2 (or prime) subscription is absent from the list; ws-2 lists its own; prime and MCP list every one. A list
    changes nothing."""
    world = await _subscription_world()
    toolset, tool, trigger_id = LIST_DOORS[door_name]
    own, _ = WS_CALLERS[who]
    before = await _subscriptions(world)

    outcome, body = await asyncio.wait_for(_call(world, toolset, tool, {"trigger_id": trigger_id}, own=own, door=door), 30)

    assert outcome == D, (door_name, who, door, body)
    assert sorted(item["id"] for item in body) == sorted(_sid(trigger_id, name) for name in VISIBLE[own]), (door_name, who, door)
    assert await _subscriptions(world) == before


_ONE_ROWS = [(d, who, door, name) for d, who, door in _rows(ONE_DOORS) for name in ("fresh-ws2", "append-ws2", "parked-ws2")]


@pytest.mark.parametrize(("door_name", "who", "door", "name"), _ONE_ROWS)
async def test_get_and_delete_of_one_subscription_reach_only_what_the_caller_reaches(door_name, who, door, name):
    """Rulings 1 and 2: the subscription targets ws-2. From ws-1, get and delete answer exactly as an unknown subscription id
    (``subscription_not_found``, the id) and nothing is deleted; from ws-2, from prime and with no session the tool acts."""
    world = await _subscription_world()
    toolset, tool, trigger_id = ONE_DOORS[door_name]
    own, expected = WS_CALLERS[who]
    expected = "subscription_not_found" if expected == N else expected
    sid = _sid(trigger_id, name)
    before = await _subscriptions(world)

    outcome, body = await asyncio.wait_for(
        _call(world, toolset, tool, {"trigger_id": trigger_id, "subscription_id": sid}, own=own, door=door), 30,
    )

    assert outcome == expected, (door_name, who, door, name, body)
    after = await _subscriptions(world)
    if outcome == D:
        if tool == "get_subscription":
            assert body["id"] == sid and after == before, body
        else:
            assert body == {"ok": True} and set(before) - set(after) == {sid} and len(after) == len(before) - 1
        return
    assert after == before, f"{door_name} changed the subscriptions on a refusal ({who}, {door}, {name})"
    assert body == {"type": "subscription_not_found", "message": sid}
    _, unknown = await _call(world, toolset, tool, {"trigger_id": trigger_id, "subscription_id": "sb-nope"}, own=own, door=door)
    assert json.dumps(body).replace(sid, "<id>") == json.dumps(unknown).replace("sb-nope", "<id>"), (body, unknown)


@pytest.mark.parametrize("door_name", list(ONE_DOORS))
@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("name", ["fresh-ws1", "append-ws1"])
async def test_get_and_delete_from_outside_prime_act_on_a_subscription_in_its_own_workspace(door_name, door, name):
    world = await _subscription_world()
    toolset, tool, trigger_id = ONE_DOORS[door_name]
    sid = _sid(trigger_id, name)

    outcome, body = await asyncio.wait_for(
        _call(world, toolset, tool, {"trigger_id": trigger_id, "subscription_id": sid}, own="ws-1", door=door), 30,
    )

    assert outcome == D, body
    present = sid in await _subscriptions(world)
    assert (body["id"] == sid and present) if tool == "get_subscription" else (body == {"ok": True} and not present), body


@pytest.mark.parametrize("door_name", list(ONE_DOORS))
@pytest.mark.parametrize("own", ["ws-1", "ws-2"])
async def test_a_subscription_whose_target_session_is_gone_is_out_of_reach_from_outside_prime(door_name, own):
    """The save check's classification: a session that does not exist is in no workspace, so get and delete answer as an unknown id for every
    person who is not an admin (an admin holds every level and acts)."""
    world = await _subscription_world()
    toolset, tool, trigger_id = ONE_DOORS[door_name]
    sid = _sid(trigger_id, "append-gone")

    refused = await _call(world, toolset, tool, {"trigger_id": trigger_id, "subscription_id": sid}, own=own, door="tool manager")
    acted = await _call(world, toolset, tool, {"trigger_id": trigger_id, "subscription_id": sid}, own="ws-prime", door="tool manager", run_as=ADMIN)

    assert refused == ("subscription_not_found", {"type": "subscription_not_found", "message": sid})
    assert acted[0] == D, acted


@pytest.mark.parametrize("door_name", list(LIST_DOORS))
@pytest.mark.parametrize(("own", "door"), [("ws-1", "tool manager"), ("ws-1", "system__call_tool"), ("ws-prime", "tool manager"), (None, "MCP")])
async def test_a_filtered_list_of_more_than_two_pages_skips_and_repeats_no_subscription(door_name, own, door):
    """Ruling 5: the service reads a trigger's subscriptions in pages of ``_LIST_PAGE_SIZE``. With more than two pages of rows the person holds
    full on and rows they do not, interleaved, a session in ws-1 (u-user: ws-1 only) gets every row it reaches, once, in storage order (no page
    short of reachable rows ends the read, and none is skipped); a session in prime and the MCP call (u-ada: ws-2 and prime) get theirs."""
    world = await _seed_world_for_subscriptions()
    toolset, tool, trigger_id = LIST_DOORS[door_name]
    cycle = [
        ("u-user", AgentFreshSubConfig(workspace_id="ws-1", agent_id="planner")),
        ("u-ada", AgentFreshSubConfig(workspace_id="ws-2", agent_id="planner")),
        ("u-ada", SessionAppendSubConfig(session_id="ses-t")),
        ("u-user", SessionAppendSubConfig(session_id="ses-a")),
        ("u-ada", AgentFreshSubConfig(workspace_id="ws-prime", agent_id="planner")),
    ]
    store = world.sp.get_storage(Subscription)
    seeded: list[tuple[str, str]] = []
    for i in range(2 * _LIST_PAGE_SIZE + 7):
        where, config = cycle[i % len(cycle)]
        sid = f"sb-{i:04d}"
        await store.create(Subscription(id=sid, trigger_id=trigger_id, config=config, created_at=NOW, owner=USER))
        seeded.append((sid, where))

    outcome, body = await asyncio.wait_for(_call(world, toolset, tool, {"trigger_id": trigger_id}, own=own, door=door), 60)

    assert outcome == D, body
    wanted = [sid for sid, holder in seeded if holder == ("u-user" if own == "ws-1" else "u-ada")]
    assert [item["id"] for item in body] == wanted


async def test_the_rest_subscription_routes_serve_and_delete_whatever_the_target(app, client, fake_storage_provider):
    """Ruling 4: a person over REST reaches every workspace, so the REST subscription routes pass no reach and list, get and delete a subscription
    whatever its target (here a user-role account)."""
    store = fake_storage_provider.get_storage(Subscription)
    await fake_storage_provider.get_storage(Trigger).create(Trigger(
        id=TRIGGER, slug="tr-one", name="t", created_at=NOW, owner=USER, config=DelayedTriggerConfig(fire_at=NOW + timedelta(days=1)),
    ))
    for name in ("fresh-ws1", "fresh-ws2", "fresh-prime"):
        await store.create(Subscription(id=_sid(TRIGGER, name), trigger_id=TRIGGER, config=TARGETS[name], created_at=NOW, owner=USER))
    r = await client.post("/v1/admin/users", json={"username": "carol", "password": "carolpw1"})
    assert r.status_code == 201, r.text
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as carol:
        assert (await carol.post("/v1/auth/login", json={"username": "carol", "password": "carolpw1"})).status_code == 200
        listed = await carol.get(f"/v1/triggers/{TRIGGER}/subscriptions")
        got = await carol.get(f"/v1/triggers/{TRIGGER}/subscriptions/{_sid(TRIGGER, 'fresh-ws2')}")
        deleted = await carol.delete(f"/v1/triggers/{TRIGGER}/subscriptions/{_sid(TRIGGER, 'fresh-prime')}")

    assert listed.status_code == 200, listed.text
    assert sorted(item["id"] for item in listed.json()["items"]) == sorted(_sid(TRIGGER, n) for n in ("fresh-ws1", "fresh-ws2", "fresh-prime"))
    assert (got.status_code, got.json()["id"]) == (200, _sid(TRIGGER, "fresh-ws2"))
    assert deleted.status_code == 204, deleted.text
    assert await store.get(_sid(TRIGGER, "fresh-prime")) is None


# -- a delete is fenced against a session started meanwhile (review of #874, D1) ---------------------------------------------------------------
#
# Once ``destroy`` marks the workspace as being destroyed, and before anything is torn down, a session start refuses the workspace. (The rank
# scan that used to run in that window is gone: the delete needs owner on the workspace, access-control P2.)


async def test_a_session_started_while_a_delete_runs_is_refused():
    """Probe D of the #874 review: a session start that lands once the workspace is marked as being destroyed is refused (as a workspace that
    does not exist), so the delete ends no session started under it."""
    from primer.workspace.session_factory import SessionFactoryDeps, start_workspace_session

    world = await _ws_world()
    await world.sp.get_storage(Agent).create(Agent(access=Access(public=True), id="planner", description="x", model=AgentModel(profile_id="p--m"), tools=[], system_prompt=[]))
    admin_start: dict = {}

    real_mark = world.workspaces._mark_terminating

    async def _an_admin_starts_a_session_then_mark(workspace_id: str):
        try:
            admin_start["session"] = await start_workspace_session(
                workspace_id="ws-2", binding=AgentSessionBinding(agent_id="planner"), initial_instructions="hi", graph_input=None,
                auto_start=False, metadata=None, parent_session_id=None, initiated_by=ADMIN,
                deps=SessionFactoryDeps(storage_provider=world.sp, claim_engine=FakeEngine(), scheduler=FakeScheduler(), workspace_registry=world.workspaces),
            )
        except NotFoundError as exc:
            admin_start["refused"] = exc.message
        return await real_mark(workspace_id)

    world.workspaces._mark_terminating = _an_admin_starts_a_session_then_mark  # type: ignore[method-assign]
    await world.workspaces.destroy("ws-2")

    assert admin_start == {"refused": "Workspace 'ws-2' does not exist"}, admin_start
    assert await world.sp.get_storage(Workspace).get("ws-2") is None
    sessions = (await world.sp.get_storage(WorkspaceSession).list(OffsetPage(offset=0, length=100))).items
    assert [s.id for s in sessions if s.initiated_by == ADMIN] == [], "the delete ended an admin's session it never scanned"


# -- the open-session scan fails loud (review of #874, E1) -------------------------------------------------------------------------------------
#
# ``open_sessions_on`` was the delete's rank scan; the delete no longer scans (owner decides, access-control P2), and the reader keeps its rule: a
# scan that cannot see every session fails loud.


async def test_open_sessions_on_raises_on_a_repeated_cursor():
    """Probe E of the #874 review: a storage that hands back a cursor it already gave ends the scan as if it were complete, so the session on the
    page it never read is never seen: the scan fails loud instead."""
    from primer.model.except_ import PrimerError
    from primer.workspace.session_reconcile import open_sessions_on

    pages = [SimpleNamespace(items=["s1"], next_cursor="c1"), SimpleNamespace(items=["s2"], next_cursor="c1"), SimpleNamespace(items=["s3"], next_cursor=None)]
    asked: list = []

    class _Sessions:
        async def find(self, predicate, page):
            asked.append(page.cursor)
            return pages[len(asked) - 1]

    seen: list = []
    with pytest.raises(PrimerError, match="repeated"):
        async for row in open_sessions_on(SimpleNamespace(get_storage=lambda cls: _Sessions()), "ws-2"):
            seen.append(row)
    assert seen == ["s1", "s2"] and asked == [None, "c1"]


# -- one scope for every identity a holder-less manager builds (review of #874, G1) ----------------------------------------------------------


async def test_a_holder_less_graph_manager_names_its_session_on_events_approvals_and_its_scope(monkeypatch, tmp_path):
    """Probe G of the #874 review: the run_scope of a graph session with no holder slot reached ``context_for`` only; the ``tool.called`` and
    ``llm.called`` events and the approval gate's park still named no session (an approval parked under ``tool_approval:unknown:...``). One scope
    now feeds them all. Built through the worker's own ``WorkerPool._build_executor``."""
    import primer.worker.executor_builders as builders
    from primer.agent.approval import ApprovalResolver
    from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
    from primer.model.yield_ import YieldToWorker

    class _Recorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def emit(self, event_type: str, **kwargs) -> None:
            self.events.append((event_type, kwargs))

    recorder = _Recorder()
    monkeypatch.setattr(builders, "_pool_event_recorder", lambda pool: recorder)
    world = await _ws_world()
    policies = world.sp.get_storage(ToolApprovalPolicy)
    await policies.create(ToolApprovalPolicy(id="gate-get", toolset_id="workspaces", tool_name="get_workspace", approval=RequiredApprovalConfig()))
    graph = _runnable_graph()
    row = WorkspaceSession(
        id="ses-g", workspace_id="ws-1", binding=GraphSessionBinding(graph_id=graph.id, graph_snapshot=graph), status=SessionStatus.RUNNING,
        created_at=NOW, initiated_by=USER,
    )
    await world.sp.get_storage(WorkspaceSession).create(row)
    await world.sp.get_storage(User).create(User(id="u-user", username="user", created_at=NOW, role="user"))
    repo = LocalStateRepo(tmp_path / "state", workspace_id="ws-1")
    await repo.initialize()
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1), scheduler=None, storage=world.sp, workspace_registry=world.workspaces,  # type: ignore[arg-type]
        provider_registry=world.registry, engine=InMemoryClaimEngine(adapters={}), approval_resolver=ApprovalResolver(policies),  # type: ignore[arg-type]
    )
    driver = await pool._build_executor(row, _HolderlessHost("ws-1", repo))  # noqa: SLF001 -- the worker's own build seam
    agent = Agent(access=Access(public=True), 
        id="planner", description="x", model=AgentModel(profile_id="p--m"), tools=["workspaces__list_workspaces", "workspaces__get_workspace"],
        system_prompt=[],
    )
    manager = await driver._executor._tool_manager_resolver(agent)  # noqa: SLF001

    assert manager.workspace_session_scope == ("ses-g", "ws-1"), "llm.called reads its session and workspace here"
    await asyncio.wait_for(manager.execute(ToolCallPart(id="call-1", name="workspaces__list_workspaces", arguments={})), 30)
    assert [(t, kw.get("session_id"), kw.get("workspace_id")) for t, kw in recorder.events if t == "tool.called"] == [("tool.called", "ses-g", "ws-1")]
    with pytest.raises(YieldToWorker) as parked:
        await asyncio.wait_for(manager.execute(ToolCallPart(id="call-2", name="workspaces__get_workspace", arguments={"id": "ws-1"})), 30)
    assert parked.value.yielded.event_key == "tool_approval:ses-g:call-2"
    assert [kw.get("session_id") for t, kw in recorder.events if t == "approval.requested"] == ["ses-g"]
