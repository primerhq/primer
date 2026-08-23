"""Approver routing on tool approvals (wiring plan P6 T13).

The park stamps a resolved ApproverSpec (policy row default, or the
Rego/LLM verdict's per-call override); respond enforces it with 403
``approver_mismatch``; the decision payload carries ``decided_by`` into
the durable record; pending/respond/records are user-tier while policy
CRUD stays admin.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from primer.agent.approval import ApprovalVerdict, effective_approvers
from primer.agent.approval_record import record_from_parked_blob
from primer.auth.passwords import hash_password
from primer.model.tool_approval import (
    ApproverSpec,
    RequiredApprovalConfig,
    ToolApprovalPolicy,
)
from primer.model.user import User
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from tests.api.conftest import raw_client as client, app, fake_provider_registry  # noqa: F401


def test_approver_spec_allows() -> None:
    anyone = ApproverSpec()
    assert anyone.allows(username="bob", role="user")

    roles = ApproverSpec(kind="roles", roles=["user"])
    assert roles.allows(username="bob", role="user")
    assert not roles.allows(username="bob", role="restricted")

    users = ApproverSpec(kind="users", users=["alice"])
    assert users.allows(username="alice", role="user")
    assert not users.allows(username="bob", role="user")
    # Admins always pass: they can edit the policy anyway, and a
    # users-routed park whose named user is gone must never wedge.
    assert users.allows(username="root", role="admin")


def test_effective_approvers_prefers_the_verdict() -> None:
    policy = ToolApprovalPolicy(
        toolset_id="_system", tool_name="x",
        approval=RequiredApprovalConfig(),
        approvers=ApproverSpec(kind="roles", roles=["user"]),
        created_at=datetime.now(timezone.utc),
    )
    per_call = ApproverSpec(kind="users", users=["alice"])
    assert effective_approvers(
        policy, ApprovalVerdict(required=True, approvers=per_call)
    ) is per_call
    assert effective_approvers(
        policy, ApprovalVerdict(required=True)
    ) is policy.approvers


def test_record_carries_decided_by() -> None:
    blob = {
        "tool_call_id": "tc",
        "yielded": {"resume_metadata": {
            "original_call": {"id": "tc", "name": "rm", "arguments": {}},
        }},
    }
    rec = record_from_parked_blob(
        blob=blob, decision="approved", reason=None, decided_by="alice",
    )
    assert rec.decided_by == "alice"


def _parked_session(
    *, session_id: str, tool_call_id: str, approvers: dict | None,
) -> WorkspaceSession:
    now = datetime.now(UTC)
    ek = f"tool_approval:{session_id}:{tool_call_id}"
    metadata: dict = {
        "original_call": {
            "id": tool_call_id, "name": "delete_workspace",
            "arguments": {"id": "ws-x"},
        },
    }
    if approvers is not None:
        metadata["approvers"] = approvers
    return WorkspaceSession(
        id=session_id, workspace_id="ws",
        binding=AgentSessionBinding(kind="agent", agent_id="agt"),
        status=SessionStatus.RUNNING, created_at=now,
        parked_status="parked", parked_at=now, parked_event_key=ek,
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {
                "tool_name": "_approval", "event_key": ek,
                "resume_metadata": metadata,
            },
        },
    )


async def _register_admin(client) -> None:
    reg = await client.post(
        "/v1/auth/register",
        json={"username": "aradmin", "password": "aradminpass1"},
    )
    assert reg.status_code == 200, reg.text


async def _login_user(client, app, username: str) -> None:
    storage = app.state.storage_provider.get_storage(User)
    await storage.create(User(
        id="user-" + username, username=username,
        password_hash=await hash_password(username + "pass1"),
        created_at=datetime.now(timezone.utc), role="user",
    ))
    login = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": username + "pass1"},
    )
    assert login.status_code == 200, login.text


@pytest.mark.asyncio
async def test_respond_enforces_the_spec_and_stamps_decided_by(
    client, app,
) -> None:
    await _register_admin(client)
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked_session(
        session_id="ar-1", tool_call_id="tc-1",
        approvers={"kind": "users", "users": ["alice"]},
    ))

    # The pending envelope surfaces the routing to the console.
    pend = await client.get("/v1/sessions/ar-1/tool_approval/pending")
    assert pend.status_code == 200, pend.text
    assert pend.json()["approvers"] == {
        "kind": "users", "roles": [], "users": ["alice"],
    } or pend.json()["approvers"] == {"kind": "users", "users": ["alice"]}

    # bob is a plain user and not the routed approver: refused.
    await _login_user(client, app, "bob")
    refused = await client.post(
        "/v1/sessions/ar-1/tool_approval/respond",
        json={"tool_call_id": "tc-1", "decision": "approved"},
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["extensions"]["error"] == "approver_mismatch"

    # alice is the routed approver; her name rides the wake payload.
    await _login_user(client, app, "alice")
    ok = await client.post(
        "/v1/sessions/ar-1/tool_approval/respond",
        json={"tool_call_id": "tc-1", "decision": "approved",
              "reason": "fine"},
    )
    assert ok.status_code == 202, ok.text
    row = await storage.get("ar-1")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"]["decided_by"] == "alice"


@pytest.mark.asyncio
async def test_users_can_decide_unrouted_parks_but_not_edit_policies(
    client, app,
) -> None:
    await _register_admin(client)
    storage = app.state.storage_provider.get_storage(WorkspaceSession)
    await storage.create(_parked_session(
        session_id="ar-2", tool_call_id="tc-2", approvers=None,
    ))

    await _login_user(client, app, "carol")
    # No spec = anyone: the user tier decides (this was admin-only).
    ok = await client.post(
        "/v1/sessions/ar-2/tool_approval/respond",
        json={"tool_call_id": "tc-2", "decision": "rejected"},
    )
    assert ok.status_code == 202, ok.text
    # The records read is user-tier too.
    recs = await client.get("/v1/tool_approval/records")
    assert recs.status_code == 200, recs.text
    # Policy CRUD stays admin.
    denied = await client.post(
        "/v1/tool_approval_policies",
        json={
            "toolset_id": "_system", "tool_name": "x",
            "approval": {"type": "required"},
        },
    )
    assert denied.status_code == 403, denied.text
