"""A REST approval records the decider and resumes the existing execution person.

The real worker builder and approved inner dispatch run against in-memory
storage. No model, workspace process, server socket or external service runs.
"""

import json
from types import SimpleNamespace

import pytest

from primer.authz import RunRefused, WorkspaceAccessRevoked
from primer.model.access import Access
from primer.model.agent import Agent
from primer.model.except_ import NotFoundError
from primer.model.model_profile import ModelProfile
from primer.model.principal import PrincipalRef
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.user import User
from primer.model.workspace import Workspace
from primer.model.workspace_session import WorkspaceSession
from primer.model.chat import ToolCallPart
from primer.toolset.system import SYSTEM_TOOLSET_ID, build_system_toolset
from primer.worker.executor_builders import resolve_run_principal
from primer.worker.yield_runtime import resume_call_tool_dispatch
from tests._support.agent_check_rows import agent_body, profile_row
from tests.api.test_session_access_routes import (
    HUMAN_TC,
    SID,
    _dave_ref,
    _parked_row,
    _workspace,
    cast as cast,
    world as world,
)
from tests.worker.test_resume_attribution import (
    _FakeAgentSessionForBuild,
    _FakeWorkspaceForBuild,
    _build_pool,
)


async def _approved(app, client, cast, *, peer=False, lower_approver=False):
    sp = app.state.storage_provider
    users = (await sp.get_storage(User).find(None, OffsetPage(offset=0, length=100))).items
    admin = next(u for u in users if u.username == "testuser")
    admin_ref = PrincipalRef(type="user", id=admin.id, user_id=admin.id,
                             display=admin.username, role="admin", source="local")
    original = _dave_ref(cast).model_copy(update={"user_id": cast.dave_id})
    person = original
    if peer:
        person = original.model_copy(update={"id": cast.carol_id, "user_id": cast.carol_id, "display": "carol"})
    elif lower_approver:
        person = admin_ref
    await sp.get_storage(Workspace).create(_workspace(
        [cast.carol_id], {cast.dave_id: "full"},
    ))
    await sp.get_storage(ModelProfile).create(profile_row("mp-1"))
    driver_agent = Agent.model_validate(agent_body("approval-driver"))
    driver_agent.access = Access(public=True)
    await sp.get_storage(Agent).create(driver_agent)
    original_call = ToolCallPart(id=HUMAN_TC, name="create_agent", arguments={"entity": agent_body("created-agent")})
    row = _parked_row(SID, by=original, tool="_approval", metadata={
        "gate_id": "1" * 32, "original_call": original_call.model_dump(mode="json"),
        "approvers": {"kind": "users", "users": ["dave"]} if lower_approver else {"kind": "roles", "roles": ["admin"]},
    })
    row.binding = row.binding.model_copy(update={"agent_id": "approval-driver"})
    row.turn_person = person
    await sp.get_storage(WorkspaceSession).create(row)
    approver = cast.dave if lower_approver else client
    response = await approver.post(f"/v1/sessions/{SID}/tool_approval/respond", json={
        "tool_call_id": HUMAN_TC, "gate_id": "1" * 32, "decision": "approved",
    })
    assert response.status_code == 202, response.text
    resumed = await sp.get_storage(WorkspaceSession).get(SID)
    assert resumed.initiated_by == original
    assert resumed.turn_person == person
    records = (await sp.get_storage(ToolApprovalRecord).find(None, OffsetPage(offset=0, length=10))).items
    assert len(records) == 1
    assert records[0].decision == "approved"
    assert records[0].decided_by == ("dave" if lower_approver else "testuser")
    [reservation] = resumed.responder_reservations.values()
    assert reservation.person.user_id == (cast.dave_id if lower_approver else admin.id)
    assert reservation.person != person
    registry = SimpleNamespace(storage_provider=sp)
    system = build_system_toolset(storage_provider=sp, provider_registry=registry)

    async def get_toolset(toolset_id):
        assert toolset_id == SYSTEM_TOOLSET_ID
        return system

    registry.get_toolset = get_toolset
    pool = _build_pool(sp)
    pool._provider_registry = registry
    workspace = _FakeWorkspaceForBuild(_FakeAgentSessionForBuild(SID, row.workspace_id))
    return sp, pool, workspace, resumed, person, original_call


@pytest.mark.parametrize("baseline", ["creator", "peer", "lower-approver"])
async def test_rest_approval_worker_owned_create_and_later_role_check(world, app, client, cast, baseline):
    sp, pool, workspace, resumed, person, call = await _approved(
        app, client, cast, peer=baseline == "peer", lower_approver=baseline == "lower-approver",
    )
    driver = await pool._build_executor(resumed, workspace)
    manager = driver._executor._tool_manager
    assert manager.context_for(call.id).initiated_by == person
    result = await resume_call_tool_dispatch(
        via={"toolset_id": SYSTEM_TOOLSET_ID}, original_call=call, tool_manager=manager,
    )
    assert not result.error, result.output
    created = await sp.get_storage(Agent).get("created-agent")
    assert created.access.owners == [person.user_id]
    # A later call in this continuation has the parked person's role, not the
    # approver's. This also pins that a lower-ranked approver cannot downscope it.
    later = await resume_call_tool_dispatch(
        via={"toolset_id": SYSTEM_TOOLSET_ID},
        original_call=ToolCallPart(id="later", name="list_llm_providers", arguments={}),
        tool_manager=manager,
    )
    if baseline == "lower-approver":
        assert not later.error, later.output
    else:
        assert later.error
        assert json.loads(later.output)["type"] == "forbidden"
    private = Agent.model_validate(agent_body("admin-private"))
    private.access = Access(owners=["different-admin"])
    await sp.get_storage(Agent).create(private)
    hidden = await resume_call_tool_dispatch(
        via={"toolset_id": SYSTEM_TOOLSET_ID},
        original_call=ToolCallPart(id="private", name="get_agent", arguments={"id": private.id}),
        tool_manager=manager,
    )
    if baseline != "lower-approver":
        assert hidden.error
        assert json.loads(hidden.output)["type"] == "not-found"


@pytest.mark.parametrize("revocation", ["disabled", "demoted", "workspace", "agent"])
async def test_approval_does_not_bypass_parked_person_revocation(world, app, client, cast, revocation):
    sp, pool, workspace, resumed, person, _ = await _approved(app, client, cast)
    if revocation in {"disabled", "demoted"}:
        user = await sp.get_storage(User).get(person.id)
        if revocation == "disabled":
            user.disabled = True
        else:
            user.role = "restricted"
        await sp.get_storage(User).update(user)
        expected = RunRefused
    elif revocation == "workspace":
        row = await sp.get_storage(Workspace).get(resumed.workspace_id)
        row.access = Access(owners=[cast.carol_id])
        await sp.get_storage(Workspace).update(row)
        expected = WorkspaceAccessRevoked
    else:
        row = await sp.get_storage(Agent).get("approval-driver")
        row.access = Access(owners=[cast.carol_id])
        await sp.get_storage(Agent).update(row)
        expected = NotFoundError
    if revocation == "demoted":
        current = await resolve_run_principal(sp, resumed)
        assert current.id == person.id
        assert current.role == "restricted"
        expected = WorkspaceAccessRevoked
    with pytest.raises(expected):
        await pool._build_executor(resumed, workspace)
    assert await sp.get_storage(Agent).get("created-agent") is None
