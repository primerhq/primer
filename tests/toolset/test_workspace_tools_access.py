"""Tools act with the person's access, not with workspace reach (access-control P2 Task 6).

A run acts as its turn's person (``person_for_tool``: the run's ``initiated_by`` when a
``ToolContext`` is present, the MCP request's actor otherwise), and every workspace tool
asks what that person holds on the named workspace instead of asking which workspace the
calling session could reach: read tools (list / get / files / sessions / transcript / log /
tap) need ``read``; write and session-control tools (create / write / steer / cancel /
interrupt / pause / resume / restart / message) need ``full``; ``delete_workspace`` needs
``owner``. A workspace the person holds no level on answers exactly as one that does not
exist (``type=not-found``); a level below the one the tool needs answers ``type=forbidden``.

The default workspace (prime) no longer reaches everything: a run in it sees only the
workspaces its person can read, and a person or an API client (no calling session, the MCP
endpoint) is checked as the token's user, never as the management plane. A trigger
subscription needs ``full`` on its target workspace for the subscription's owner, on save
and on fire.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime

import pytest

from primer.model.access import Access
from primer.model.principal import Principal, PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.trigger import AgentFreshSubConfig, DelayedTriggerConfig, Subscription, Trigger
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta, WorkspaceTemplate
from primer.model.workspace_session import SessionInfo, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.mcp.server import current_actor
from primer.toolset.workspaces import build_workspaces_toolset
from primer.trigger.reach import refuse_fire_out_of_reach
from primer.trigger.service import ServiceDeps, SubscriptionTargetNotFound, TriggerNotFound, create_subscription
from tests.session._peer_fakes import FakeWorkspace, make_world, seed_session

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
U1 = PrincipalRef(type="user", id="u1", display="u1", role="user", source="local")
U2 = PrincipalRef(type="user", id="u2", display="u2", role="user", source="local")
U3 = PrincipalRef(type="user", id="u3", display="u3", role="user", source="local")
ADMIN = PrincipalRef(type="user", id="admin-1", display="admin-1", role="admin", source="local")


def _ctx(session_id: str, workspace_id: str, who: PrincipalRef) -> ToolContext:
    return ToolContext(tool_call_id="c1", session_id=session_id, workspace_id=workspace_id, initiated_by=who)


@contextlib.contextmanager
def _as_mcp(user_id: str):
    """The MCP endpoint's live actor: the token's user (access is decided by the owner, never by the token)."""
    token = current_actor.set(
        Principal(type="user", id=f"tok-{user_id}", display=f"tok-{user_id}", role="user", source="local", user_id=user_id)
    )
    try:
        yield
    finally:
        current_actor.reset(token)


class _Files(FakeWorkspace):
    """A live workspace with a one-file filesystem: what the file tools of this suite read and write."""

    def __init__(self, world, workspace_id: str) -> None:
        super().__init__()
        self.world = world
        self.workspace_id = workspace_id
        self.written: list[tuple[str, bytes]] = []
        self.deleted: list[str] = []
        self.list_calls: list[dict] = []

    async def read_file(self, path: str) -> bytes:
        if path != "notes.txt":
            raise FileNotFoundError(f"no file {path!r}")
        return b"shared notes"

    async def write_file(self, path: str, raw: bytes) -> None:
        self.written.append((path, raw))

    async def list_files(self, path: str = "", *, recursive: bool = False, max_entries: int | None = None, skip_reserved: bool = False) -> list:
        self.list_calls.append({"recursive": recursive, "skip_reserved": skip_reserved})
        return []

    async def delete_file(self, path: str) -> None:
        self.deleted.append(path)

    async def log(self, limit: int | None = None) -> list:
        return []

    async def list_sessions(self) -> list[SessionInfo]:
        page = await self.world.sp.get_storage(WorkspaceSession).find(None, OffsetPage(offset=0, length=200))
        return [
            SessionInfo(
                session_id=row.id,
                agent_id="agent-" + row.id,
                workspace_id=row.workspace_id,
                status=row.status,
                started_at=row.created_at,
                last_activity_at=row.created_at,
            )
            for row in page.items
            if row.workspace_id == self.workspace_id
        ]


class _Registry:
    """The workspaces toolset's registry: the live workspaces of the world plus the row writes the create and delete tools make."""

    def __init__(self, world) -> None:
        self.world = world
        self.looked_up: list[str] = []
        self.workspaces: dict[str, _Files] = {}

    def _live(self, workspace_id: str) -> _Files:
        if workspace_id in self.world.sp.get_storage(Workspace)._data:
            return self.workspaces.setdefault(workspace_id, _Files(self.world, workspace_id))
        raise FileNotFoundError(f"Workspace {workspace_id!r} does not exist")

    async def get_workspace(self, workspace_id: str) -> _Files:
        self.looked_up.append(workspace_id)
        return self._live(workspace_id)

    async def get_workspace_row(self, workspace_id: str):
        from types import SimpleNamespace

        return SimpleNamespace(phase="ready")

    async def create(self, *, template, overrides=None, workspace_id=None, name=None, reply_binding=None, access=None, person=None):
        row = Workspace(
            id=workspace_id,
            name=name,
            template_id=template.id,
            provider_id=template.provider_id,
            created_at=NOW,
            phase="pending",
            runtime_meta=WorkspaceRuntimeMeta(url=f"ws://{workspace_id}", token="t"),
            access=access or Access(),
        )
        await self.world.sp.get_storage(Workspace).create(row)
        return row

    async def destroy(self, workspace_id: str) -> None:
        await self.world.sp.get_storage(Workspace).delete(workspace_id)
        self.workspaces.pop(workspace_id, None)


async def _world() -> tuple:
    world = make_world(with_bus=True)
    workspaces = world.sp.get_storage(Workspace)

    def _ws(workspace_id: str, name: str, access: Access) -> Workspace:
        return Workspace(
            id=workspace_id, name=name, template_id="tpl", provider_id="local-1", created_at=NOW,
            runtime_meta=WorkspaceRuntimeMeta(url=f"ws://{workspace_id}", token="t"), access=access,
        )

    await workspaces.create(_ws("ws-1", "prime", Access(owners=["u1"])))
    await workspaces.create(_ws("ws-2", "private", Access(owners=["u2"], grants={"u3": "read"})))
    await workspaces.create(_ws("ws-pub", "public", Access(public=True)))
    await world.sp.set_default_workspace_id("ws-1")
    await world.sp.get_storage(WorkspaceTemplate).create(WorkspaceTemplate(id="tpl", provider_id="local-1", description="template", access=Access(public=True)))
    await seed_session(world, sid="ses-a", initiated_by=U1)  # ws-1 (the default)
    await seed_session(world, sid="ses-b", workspace_id="ws-2", initiated_by=U2)
    await seed_session(world, sid="ses-c", workspace_id="ws-pub", initiated_by=U2)
    await seed_session(world, sid="ses-d", workspace_id="ws-2", initiated_by=U3)
    return world


def _tools(world):
    registry = _Registry(world)
    toolset = build_workspaces_toolset(
        storage_provider=world.sp,
        workspace_registry=registry,
        scheduler=world.scheduler,
        claim_engine=world.engine,
        event_bus=world.bus,
    )
    return toolset, registry


async def _call(toolset, name: str, arguments: dict, ctx: ToolContext | None = None):
    return await toolset.call(tool_name=name, arguments=arguments, ctx=ctx)


def _type(result) -> str | None:
    return json.loads(result.output).get("type")


def _items(result) -> list[dict]:
    return json.loads(result.output)["items"]


def _ok(result, what: str) -> None:
    assert not result.is_error, f"{what}: expected success, got {result.output}"


def _refused(result, expected: str, what: str) -> None:
    assert result.is_error and _type(result) == expected, f"{what}: expected {expected}, got {result.output}"


async def test_default_workspace_no_longer_reaches_everything():
    """Review focus 2: a run in the caller's own default workspace is refused on another user's private workspace."""
    world = await _world()
    toolset, registry = _tools(world)
    out = await _call(toolset, "list_workspaces", {}, _ctx("ses-a", "ws-1", U1))
    _ok(out, "list_workspaces")
    assert {row["id"] for row in _items(out)} == {"ws-1", "ws-pub"}  # ws-2 is u2's private workspace
    out = await _call(toolset, "read_workspace_file", {"workspace_id": "ws-2", "path": "notes.txt"}, _ctx("ses-a", "ws-1", U1))
    _refused(out, "not-found", "read_workspace_file on another user's private workspace")
    assert registry.looked_up == []  # the refusal comes before the workspace is looked up


async def test_read_tools_need_read():
    world = await _world()
    toolset, _ = _tools(world)
    # No level at all: answered as missing, whatever the tool.
    _refused(await _call(toolset, "get_workspace", {"id": "ws-2"}, _ctx("ses-a", "ws-1", U1)), "not-found", "get_workspace")
    _refused(
        await _call(toolset, "list_workspace_files", {"workspace_id": "ws-2"}, _ctx("ses-a", "ws-1", U1)),
        "not-found",
        "list_workspace_files",
    )
    _refused(
        await _call(toolset, "list_workspace_sessions", {"workspace_id": "ws-2"}, _ctx("ses-a", "ws-1", U1)),
        "not-found",
        "list_workspace_sessions",
    )
    _refused(await _call(toolset, "find_sessions", {"workspace_id": "ws-2"}, _ctx("ses-a", "ws-1", U1)), "not-found", "find_sessions")
    # Public means read: u1 (no grant on ws-pub) reads it.
    out = await _call(toolset, "get_workspace", {"id": "ws-pub"}, _ctx("ses-a", "ws-1", U1))
    _ok(out, "get_workspace on a public workspace")
    assert json.loads(out.output)["is_default"] is False
    _ok(await _call(toolset, "list_workspace_files", {"workspace_id": "ws-pub"}, _ctx("ses-a", "ws-1", U1)), "list_workspace_files on a public workspace")
    out = await _call(toolset, "list_workspace_sessions", {"workspace_id": "ws-2"}, _ctx("ses-d", "ws-2", U3))
    _ok(out, "list_workspace_sessions by a read grant")
    assert {row["session_id"] for row in _items(out)} == {"ses-b", "ses-d"}
    # find_sessions lists across the workspaces the person can read.
    out = await _call(toolset, "find_sessions", {}, _ctx("ses-a", "ws-1", U1))
    _ok(out, "find_sessions")
    assert {row["id"] for row in _items(out)} == {"ses-a", "ses-c"}
    out = await _call(toolset, "find_sessions", {}, _ctx("ses-b", "ws-2", U2))
    _ok(out, "find_sessions as u2")
    assert {row["id"] for row in _items(out)} == {"ses-b", "ses-d", "ses-c"}
    out = await _call(toolset, "find_sessions", {}, _ctx("ses-a", "ws-1", ADMIN))
    _ok(out, "find_sessions as an admin")
    assert {row["id"] for row in _items(out)} == {"ses-a", "ses-b", "ses-c", "ses-d"}


async def test_session_control_tools_need_full():
    world = await _world()
    toolset, _ = _tools(world)
    # Public is read: a write on ws-pub is refused (type=forbidden) by u1; no level on ws-2 answers as missing.
    _refused(
        await _call(
            toolset,
            "create_workspace_session",
            {"workspace_id": "ws-pub", "binding": {"kind": "agent", "agent_id": "planner"}},
            _ctx("ses-a", "ws-1", U1),
        ),
        "forbidden",
        "create_workspace_session on a public workspace",
    )
    _refused(await _call(toolset, "cancel_workspace_session", {"workspace_id": "ws-pub", "session_id": "ses-c"}, _ctx("ses-a", "ws-1", U1)), "forbidden", "cancel on a public workspace")
    _refused(await _call(toolset, "interrupt_workspace_session", {"workspace_id": "ws-pub", "session_id": "ses-c"}, _ctx("ses-a", "ws-1", U1)), "forbidden", "interrupt on a public workspace")
    _refused(await _call(toolset, "pause_workspace_session", {"workspace_id": "ws-pub", "session_id": "ses-c"}, _ctx("ses-a", "ws-1", U1)), "forbidden", "pause on a public workspace")
    _refused(await _call(toolset, "resume_workspace_session", {"workspace_id": "ws-pub", "session_id": "ses-c"}, _ctx("ses-a", "ws-1", U1)), "forbidden", "resume on a public workspace")
    _refused(
        await _call(toolset, "steer_workspace_session", {"workspace_id": "ws-pub", "session_id": "ses-c", "instruction": "go"}, _ctx("ses-a", "ws-1", U1)),
        "forbidden",
        "steer on a public workspace",
    )
    _refused(
        await _call(toolset, "restart_workspace_session", {"workspace_id": "ws-pub", "session_id": "ses-c", "input": "again"}, _ctx("ses-a", "ws-1", U1)),
        "forbidden",
        "restart on a public workspace",
    )
    # No level at all: answered as missing, not forbidden.
    _refused(await _call(toolset, "cancel_workspace_session", {"workspace_id": "ws-2", "session_id": "ses-b"}, _ctx("ses-a", "ws-1", U1)), "not-found", "cancel on another user's private workspace")
    # A read grant (u3 on ws-2) cannot steer or cancel.
    _refused(await _call(toolset, "cancel_workspace_session", {"workspace_id": "ws-2", "session_id": "ses-b"}, _ctx("ses-d", "ws-2", U3)), "forbidden", "cancel with a read grant")
    _refused(
        await _call(toolset, "steer_workspace_session", {"workspace_id": "ws-2", "session_id": "ses-b", "instruction": "go"}, _ctx("ses-d", "ws-2", U3)),
        "forbidden",
        "steer with a read grant",
    )
    # The owner acts: u2 cancels u2's session.
    out = await _call(toolset, "cancel_workspace_session", {"workspace_id": "ws-2", "session_id": "ses-b"}, _ctx("ses-b", "ws-2", U2))
    _ok(out, "cancel by the owner")
    assert json.loads(out.output)["status"] == "ended"
    # A message from a session is a peer message: the sender's person needs full on the target's workspace.
    _refused(
        await _call(toolset, "steer_workspace_session", {"workspace_id": "ws-2", "session_id": "ses-b", "instruction": "go"}, _ctx("ses-a", "ws-1", U1)),
        "not-found",
        "steer from a session in another workspace",
    )
    _refused(
        await _call(toolset, "message_session", {"session_id": "ses-b", "text": "hello"}, _ctx("ses-a", "ws-1", U1)),
        "not-found",
        "message_session to another workspace",
    )
    # With no calling session (the MCP endpoint) the token's user is checked.
    with _as_mcp("u1"):
        _refused(
            await _call(toolset, "steer_workspace_session", {"workspace_id": "ws-2", "session_id": "ses-b", "instruction": "go"}),
            "not-found",
            "steer as the token's user without a level",
        )
        _refused(
            await _call(toolset, "steer_workspace_session", {"workspace_id": "ws-2", "session_id": "ses-b", "instruction": "go"}, None),
            "not-found",
            "steer as the token's user (explicit None ctx)",
        )
    with _as_mcp("u3"):
        _refused(
            await _call(toolset, "steer_workspace_session", {"workspace_id": "ws-2", "session_id": "ses-b", "instruction": "go"}),
            "forbidden",
            "steer as the token's user with a read grant",
        )
    with _as_mcp("u2"):
        # A live session (the cancelled ses-b would refuse a machine steer: an operator's Cancel sticks to one nobody's turn reopens).
        out = await _call(toolset, "steer_workspace_session", {"workspace_id": "ws-2", "session_id": "ses-d", "instruction": "go"})
        _ok(out, "steer as the token's user (the owner)")
        assert json.loads(out.output)["delivery"] == "woken"


async def test_file_tools_need_their_level():
    world = await _world()
    toolset, _ = _tools(world)
    # Read level: u1 reads the public workspace's file, not u2's private one.
    _refused(await _call(toolset, "read_workspace_file", {"workspace_id": "ws-2", "path": "notes.txt"}, _ctx("ses-a", "ws-1", U1)), "not-found", "read_workspace_file on a private workspace")
    out = await _call(toolset, "read_workspace_file", {"workspace_id": "ws-pub", "path": "notes.txt"}, _ctx("ses-a", "ws-1", U1))
    _ok(out, "read_workspace_file on a public workspace")
    assert json.loads(out.output)["content"] == "shared notes"
    # Write level: the read-only u1 cannot write or delete in ws-pub.
    _refused(await _call(toolset, "write_workspace_file", {"workspace_id": "ws-pub", "path": "notes.txt", "content": "no"}, _ctx("ses-a", "ws-1", U1)), "forbidden", "write_workspace_file on a public workspace")
    _refused(await _call(toolset, "delete_workspace_file", {"workspace_id": "ws-pub", "path": "notes.txt"}, _ctx("ses-a", "ws-1", U1)), "forbidden", "delete_workspace_file on a public workspace")
    _refused(await _call(toolset, "get_workspace_log", {"workspace_id": "ws-2"}, _ctx("ses-a", "ws-1", U1)), "not-found", "get_workspace_log on a private workspace")
    # The owner writes.
    out = await _call(toolset, "write_workspace_file", {"workspace_id": "ws-2", "path": "notes.txt", "content": "yours"}, _ctx("ses-b", "ws-2", U2))
    _ok(out, "write_workspace_file by the owner")


async def test_delete_workspace_needs_owner():
    world = await _world()
    toolset, _ = _tools(world)
    _refused(await _call(toolset, "delete_workspace", {"id": "ws-pub"}, _ctx("ses-a", "ws-1", U1)), "forbidden", "delete_workspace with a read level")
    _refused(await _call(toolset, "delete_workspace", {"id": "ws-2"}, _ctx("ses-d", "ws-2", U3)), "forbidden", "delete_workspace with a read grant")
    out = await _call(toolset, "delete_workspace", {"id": "ws-2"}, _ctx("ses-b", "ws-2", U2))
    _ok(out, "delete_workspace by the owner")
    assert json.loads(out.output) == {"deleted": True, "id": "ws-2"}
    assert await world.sp.get_storage(Workspace).get("ws-2") is None


async def test_mcp_caller_is_checked_as_the_tokens_user():
    world = await _world()
    toolset, _ = _tools(world)
    with _as_mcp("u2"):
        _ok(await _call(toolset, "get_workspace", {"id": "ws-2"}), "get_workspace as the token's user (the owner)")
        out = await _call(toolset, "list_workspaces", {})
        _ok(out, "list_workspaces as the token's user")
        assert {row["id"] for row in _items(out)} == {"ws-2", "ws-pub"}
    with _as_mcp("u1"):
        _refused(await _call(toolset, "get_workspace", {"id": "ws-2"}), "not-found", "get_workspace as the token's user without a level")
        out = await _call(toolset, "list_workspaces", {})
        _ok(out, "list_workspaces as the token's user")
        assert {row["id"] for row in _items(out)} == {"ws-1", "ws-pub"}
    # A create stamps the creator: the token's user over MCP, the run's person from a session.
    with _as_mcp("u2"):
        out = await _call(toolset, "create_workspace", {"id": "ws-new", "template_id": "tpl"})
        _ok(out, "create_workspace over MCP")
    assert (await world.sp.get_storage(Workspace).get("ws-new")).access.owners == ["u2"]
    out = await _call(toolset, "create_workspace", {"id": "ws-mine", "template_id": "tpl"}, _ctx("ses-a", "ws-1", U1))
    _ok(out, "create_workspace from a session")
    assert (await world.sp.get_storage(Workspace).get("ws-mine")).access.owners == ["u1"]


async def test_trigger_subscription_target_needs_full_on_save_and_fire():
    world = await _world()
    deps = ServiceDeps(storage_provider=world.sp, claim_engine=world.engine, event_bus=world.bus)
    await world.sp.get_storage(Trigger).create(
        Trigger(
            id="trg-1",
            slug="one",
            name="One",
            config=DelayedTriggerConfig(kind="delayed", fire_at=NOW.replace(hour=10)),
            created_at=NOW,
            owner=U2,
            access=Access(owners=[U2.id]),  # the run-as owner holds the access the fire-time check asks of them (P3 SEC)
        )
    )
    config = AgentFreshSubConfig(workspace_id="ws-2", agent_id="planner")
    # Save: the subscription's owner needs full on the target workspace.
    with pytest.raises(SubscriptionTargetNotFound):
        await create_subscription(trigger_id="trg-1", config=config, owner=U1, deps=deps)
    sub = await create_subscription(trigger_id="trg-1", config=config, owner=U2, deps=deps)
    assert sub.owner == U2
    # Fire: the recorded owner's access is asked again.
    sub_for_u1 = Subscription(id="sub-u1", trigger_id="trg-1", config=config, created_at=NOW, owner=U1)
    assert await refuse_fire_out_of_reach(sub_for_u1, world.sp) is not None
    assert await refuse_fire_out_of_reach(sub, world.sp) is None


async def test_list_my_sessions_needs_read():
    world = await _world()
    toolset, _ = _tools(world)
    # u3 holds only a read grant on ws-2: the tool still works (a read is enough to list).
    out = await _call(toolset, "list_my_sessions", {}, _ctx("ses-d", "ws-2", U3))
    _ok(out, "list_my_sessions with a read grant")
    assert {row["id"] for row in _items(out)} == {"ses-b", "ses-d"}


async def test_subscription_visibility_follows_the_callers_full_not_the_default_workspace():
    from primer.trigger.reach import caller_scope
    from primer.trigger.service import list_subscriptions

    world = await _world()
    deps = ServiceDeps(storage_provider=world.sp, claim_engine=world.engine, event_bus=world.bus)
    await world.sp.get_storage(Trigger).create(
        Trigger(id="trg-1", slug="one", name="One", config=DelayedTriggerConfig(kind="delayed", fire_at=NOW.replace(hour=10)), created_at=NOW)
    )
    await create_subscription(
        trigger_id="trg-1", config=AgentFreshSubConfig(workspace_id="ws-2", agent_id="planner"), owner=U2, deps=deps
    )

    async def _listed(who: PrincipalRef) -> int:
        reach = caller_scope(_ctx("ses-a", "ws-1", who))
        return len(await list_subscriptions(trigger_id="trg-1", reach=reach, deps=deps))

    # A run in the default workspace (prime) no longer sees every workspace's subscriptions; a read grant is not enough either.
    assert await _listed(U1) == 0
    assert await _listed(U3) == 0
    assert await _listed(U2) == 1
    assert await _listed(ADMIN) == 1


async def test_a_users_recursive_listing_asks_the_backend_to_skip_the_reserved_trees_and_an_admins_does_not():
    """W55c through the tool: a non-admin's capped walk is pruned during the walk; an admin's lists the reserved trees."""
    world = await _world()
    toolset, registry = _tools(world)

    _ok(await _call(toolset, "list_workspace_files", {"workspace_id": "ws-1", "recursive": True}, _ctx("ses-a", "ws-1", U1)), "user")
    _ok(await _call(toolset, "list_workspace_files", {"workspace_id": "ws-1", "recursive": True}, _ctx("ses-a", "ws-1", ADMIN)), "admin")

    assert registry.workspaces["ws-1"].list_calls == [
        {"recursive": True, "skip_reserved": True}, {"recursive": True, "skip_reserved": False},
    ]
