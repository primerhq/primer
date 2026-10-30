"""The session tools act by the run's person's level on the workspace, not on who started the session (access-control P2 T6).

A session runs at most at the rank recorded on its row, capped by its creator's current role. When that creator is demoted the row still
records admin while the run's person is the demoted one; the tools ask that person's level on the workspace (the access rule replaces the
rank rule the tools used to carry; the REST routes keep it in ``refuse_outranked``). A person who holds the workspace acts on every session
in it, whatever the sessions' recorded creators, and a person who holds below full is refused (forbidden), asked before the session is read.
The third test pins where the REST guard runs: on the row the helper read inside ``session_lifecycle_lock``, not on a read made before it.
(All three were the reviewer's probes on #904, the first two re-asked against the access rule.)
"""

from __future__ import annotations

import json

import pytest

from primer.agent.inner_dispatch import dispatch_inner_tool
from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext
from primer.toolset.workspaces import WORKSPACES_TOOLSET_ID, build_workspaces_toolset
from tests.api.test_session_control_rule_table import NOW, _world
from tests.session._peer_fakes import FakeScheduler, seed_session

ADA_THEN = PrincipalRef(type="user", id="u-ada", display="ada", role="admin", source="local")
ADA_NOW = ADA_THEN.model_copy(update={"role": "user"})  # what resolve_run_principal hands the run after ada's demotion
READER = PrincipalRef(type="user", id="u-user", display="user", role="user", source="local", user_id="u-user")  # holds read on ws-1 only


async def _world_with_ws1():
    """The rule-table world with the access row of #904's probe: ws-1 is u-ada's, u-user holds read on it (the level the tools ask on)."""
    world = _world()
    await world.sp.get_storage(Workspace).create(Workspace(
        id="ws-1", name="ws-1", template_id="tpl", provider_id="local-1", created_at=NOW, phase="running",
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="t"),
        access=Access(owners=["u-ada"], grants={"u-user": "read"}),
    ))
    return world

class _Scheduler(FakeScheduler):
    """The cancel of a RUNNING session also signals the legacy scheduler path."""

    def __init__(self) -> None:
        super().__init__()
        self.signalled: list[str] = []

    async def signal_cancel(self, session_id: str) -> None:
        self.signalled.append(session_id)


#: verb -> what the session's own call leaves on its RUNNING row.
LANDED = {
    "interrupt": lambda row: row.interrupt_requested is True,
    "pause": lambda row: row.pause_requested is True,
    "cancel": lambda row: row.cancel_requested is True,
    "resume": lambda row: row.status == SessionStatus.RUNNING,  # the documented no-op on a running session
}


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", list(LANDED))
async def test_a_session_whose_creator_was_demoted_acts_on_itself(verb):
    """The session's OWN run (``ctx.session_id`` is the target) after its creator was demoted: the run ranks user, the row records admin, and
    the person (the workspace's owner) holds the level the tools ask on."""
    world = await _world_with_ws1()
    world.scheduler = _Scheduler()
    await seed_session(world, sid="ses-t", workspace_id="ws-1", status=SessionStatus.RUNNING, turn_no=1, created_at=NOW, initiated_by=ADA_THEN)
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine, event_bus=world.bus,
    )
    ctx = ToolContext(tool_call_id="c1", session_id="ses-t", workspace_id="ws-1", initiated_by=ADA_NOW)

    result = await dispatch_inner_tool(
        provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name=f"{verb}_workspace_session",
        arguments={"workspace_id": "ws-1", "session_id": "ses-t"}, ctx=ctx,
    )

    assert not result.is_error, json.loads(result.output)
    assert LANDED[verb](await world.sessions.get("ses-t")), f"the session's own {verb} did not land"


@pytest.mark.asyncio
async def test_a_person_who_holds_below_full_may_not_act_on_a_session_the_owner_started():
    """The creator is no longer consulted (access-control P2 T6): a person who holds read on ws-1 is refused (forbidden) when acting on a
    session its owner started, whatever the session's recorded creator, and nothing moves."""
    world = await _world_with_ws1()
    await seed_session(world, sid="ses-t", workspace_id="ws-1", status=SessionStatus.WAITING, turn_no=1, created_at=NOW, initiated_by=ADA_THEN)
    toolset = build_workspaces_toolset(
        storage_provider=world.sp, workspace_registry=world.registry, scheduler=world.scheduler, claim_engine=world.engine, event_bus=world.bus,
    )
    before = (await world.sessions.get("ses-t")).model_dump(mode="json")
    ctx = ToolContext(tool_call_id="c1", session_id="ses-other", workspace_id="ws-1", initiated_by=READER)

    result = await dispatch_inner_tool(
        provider=toolset, toolset_id=WORKSPACES_TOOLSET_ID, tool_name="pause_workspace_session",
        arguments={"workspace_id": "ws-1", "session_id": "ses-t"}, ctx=ctx,
    )

    assert json.loads(result.output) == {"type": "forbidden", "message": "Workspace 'ws-1' needs full access"}
    assert (await world.sessions.get("ses-t")).model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_the_guard_sees_the_row_read_under_the_lock():
    """The guard is called with the row the helper read inside ``session_lifecycle_lock`` (not a pre-read): the lock is held when it runs."""
    from primer.session.mutation_lock import session_lifecycle_lock
    from primer.workspace.session_factory import SessionInterruptDeps, interrupt_session, pause_session, resume_session

    world = _world()
    await seed_session(world, sid="ses-t", workspace_id="ws-1", status=SessionStatus.WAITING, turn_no=1, created_at=NOW, initiated_by=ADA_THEN)
    seen: list[tuple[str, bool]] = []
    lock = session_lifecycle_lock()

    def guard_for(name):
        def guard(row: WorkspaceSession) -> None:
            seen.append((name, "ses-t" in lock._locks and lock._locks["ses-t"].locked()))
        return guard

    await pause_session(workspace_id="ws-1", session_id="ses-t", sessions=world.sessions, guard=guard_for("pause"))
    await resume_session(
        workspace_id="ws-1", session_id="ses-t", sessions=world.sessions, scheduler=world.scheduler, claim_engine=world.engine,
        guard=guard_for("resume"),
    )
    await interrupt_session(
        workspace_id="ws-1", session_id="ses-t", deps=SessionInterruptDeps(storage_provider=world.sp, event_bus=world.bus),
        guard=guard_for("interrupt"),
    )

    assert seen == [("pause", True), ("resume", True), ("interrupt", True)]
