"""A gate parked by ``call_tool`` is decided by its approvers over REST too (ticket 01a11b64).

The park the ``call_tool`` meta-dispatch produces used to carry no ``approvers``, so the respond route's check found nothing to enforce and
bob could approve what a policy restricted to alice. This drives the REAL park (the toolset handler raises the YieldToWorker whose metadata
is what a worker would persist), stores it on a session, and answers it as bob and as alice.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from primer.model.tool_approval import ApproverSpec, RequiredApprovalConfig, ToolApprovalPolicy
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.system import SYSTEM_TOOLSET_ID
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401
from tests.api.test_approver_routing import _login_user, _register_admin


async def _call_tool_park(app, *, approvers: ApproverSpec | None) -> dict:
    sp = app.state.storage_provider
    await sp.get_storage(ToolApprovalPolicy).create(ToolApprovalPolicy(
        id="tap-ct", toolset_id=SYSTEM_TOOLSET_ID, tool_name="list_llm_providers", enabled=True,
        approval=RequiredApprovalConfig(), approvers=approvers,
    ))
    app.state.approval_resolver.invalidate()
    ctx = ToolContext(tool_call_id="tc-ct", session_id="ct-1", workspace_id="ws", chat_id=None)
    with pytest.raises(YieldToWorker) as parked:
        await app.state.system_toolset.call(
            tool_name="call_tool",
            arguments={"toolset_id": SYSTEM_TOOLSET_ID, "tool_name": "list_llm_providers", "arguments": {}},
            ctx=ctx,
        )
    yielded = parked.value.yielded
    now = datetime.now(UTC)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="ct-1", workspace_id="ws", binding=AgentSessionBinding(kind="agent", agent_id="agt"), status=SessionStatus.RUNNING,
        created_at=now, parked_status="parked", parked_at=now, parked_event_key=yielded.event_key,
        parked_state={
            "tool_call_id": "tc-ct",
            "yielded": {"tool_name": "_approval", "event_key": yielded.event_key, "resume_metadata": yielded.resume_metadata},
        },
    ))
    return yielded.resume_metadata


@pytest.mark.asyncio
async def test_a_call_tool_gate_routed_to_alice_is_refused_to_bob_and_accepted_from_alice(client, app) -> None:
    await _register_admin(client)
    await _call_tool_park(app, approvers=ApproverSpec(kind="users", users=["alice"]))

    await _login_user(client, app, "bob")
    refused = await client.post("/v1/sessions/ct-1/tool_approval/respond", json={"tool_call_id": "tc-ct", "decision": "approved"})
    assert refused.status_code == 403, f"bob approved a gate routed to alice: {refused.text}"
    assert refused.json()["extensions"]["error"] == "approver_mismatch"

    await _login_user(client, app, "alice")
    accepted = await client.post("/v1/sessions/ct-1/tool_approval/respond", json={"tool_call_id": "tc-ct", "decision": "approved"})
    assert accepted.status_code == 202, accepted.text
    row = await app.state.storage_provider.get_storage(WorkspaceSession).get("ct-1")
    assert row.parked_state["resume_event_payload"]["decided_by"] == "alice"


@pytest.mark.asyncio
async def test_a_call_tool_gate_without_approvers_is_decided_by_any_user(client, app) -> None:
    """The control: an unrestricted policy is unchanged."""
    await _register_admin(client)
    await _call_tool_park(app, approvers=None)

    await _login_user(client, app, "bob")
    accepted = await client.post("/v1/sessions/ct-1/tool_approval/respond", json={"tool_call_id": "tc-ct", "decision": "rejected"})

    assert accepted.status_code == 202, accepted.text
