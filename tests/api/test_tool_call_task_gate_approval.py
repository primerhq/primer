from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry
from primer.auth.passwords import hash_password
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.claim.tool_call_resume import TaskGateResumeOutcome
from primer.int.claim import ClaimKind
from primer.model.access import Access
from primer.model.provider import SqliteConfig
from primer.model.storage import OffsetPage
from primer.model.tool_approval import ToolApprovalRecord
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.task_gate_runtime import TaskGateState
from tests.api.test_gate_fence import _approval_session


TOKEN = "a" * 32


@dataclass
class World:
    sp: SqliteStorageProvider
    client: httpx.AsyncClient
    app: Any
    engine: InMemoryClaimEngine


@pytest_asyncio.fixture
async def world(tmp_path) -> AsyncIterator[World]:
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "task-approval.sqlite")))
    await sp.initialize()
    await sp.get_event_store().ensure_schema()
    await sp.get_storage(Workspace).create(Workspace(
        id="ws-task", template_id="tpl", provider_id="local",
        created_at=datetime.now(UTC), access=Access(),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://127.0.0.1:5959", token=SecretStr("stub-token"),
        ),
    ))
    registry = ProviderRegistry(
        sp, llm_factory=lambda row: object(), embedder_factory=lambda row: object(),
        cross_encoder_factory=lambda row: object(), toolset_factory=lambda row: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)
    engine = InMemoryClaimEngine(adapters={
        ClaimKind.SESSION: SessionClaimAdapter(
            session_storage=sp.get_storage(WorkspaceSession),
        ),
        ClaimKind.TOOL_CALL: ToolCallClaimAdapter(
            task_storage=sp.get_storage(ToolCallTask),
        ),
    })
    app.state.claim_engine = engine
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as client:
            registered = await client.post(
                "/v1/auth/register",
                json={"username": "admin", "password": "testpassword"},
            )
            assert registered.status_code in (200, 201), registered.text
            yield World(sp=sp, client=client, app=app, engine=engine)
    finally:
        await sp.aclose()


async def _park_task_approval(
    sp: SqliteStorageProvider,
    sid: str = "task-sid",
    *,
    approvers: dict | None = None,
    task_state: ToolCallTaskState = ToolCallTaskState.GATED,
    claim_token: str | None = None,
    binding_epoch: int = 0,
    session_binding_epoch: int | None = None,
    parked_status: str | None = "parked",
    session_status: SessionStatus = SessionStatus.RUNNING,
) -> ToolCallTask:
    now = datetime.now(UTC)
    task_id = f"{sid}/x:tool:0:1"
    event_key = f"tool_approval:{sid}:wire-1"
    metadata = {
        "original_call": {
            "id": "wire-1", "name": "dangerous", "arguments": {"path": "/tmp/x"},
        },
        "policy_id": "policy-1",
        "approval_type": "required",
        "gate_reason": "matched",
        "approvers": approvers,
    }
    gate = TaskGateState(
        occurrence_token=TOKEN, gate_seq=1, event_key=event_key,
        yielded=Yielded(
            tool_name="_approval", event_key=event_key,
            resume_metadata=metadata,
        ),
        tool_call_id="wire-1", tool_arguments={"path": "/tmp/x"},
        parked_at=now, deadline=now + timedelta(minutes=5),
        binding_epoch=binding_epoch,
    )
    task = ToolCallTask(
        id=task_id, session_id=sid, turn_no=0, tool_name="dangerous",
        state=task_state, record_seq=1, call_id="wire-1",
        claim_token=claim_token,
        gate_event_key=event_key, gate_until=gate.deadline,
        gate_occurrence_token=TOKEN, gate_seq=1, gate_state=gate.to_jsonable(),
        batch_task_ids=[task_id], created_at=now,
    )
    await sp.get_storage(ToolCallTask).create(task)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=sid, workspace_id="ws-task",
        binding=AgentSessionBinding(agent_id="agent-1"),
        status=session_status, created_at=now,
        binding_epoch=(
            binding_epoch
            if session_binding_epoch is None
            else session_binding_epoch
        ),
        parked_status=parked_status, parked_at=now,
        parked_event_key=f"tool_wait:{sid}:x:tool:0:1",
        parked_state={
            "kind": "tool_wait", "outstanding_task_ids": [task_id],
            "notifying_task_ids": [],
        },
    ))
    return task


async def _login_user(world: World, username: str, *, access: str) -> None:
    user_id = f"user-{username}"
    await world.sp.get_storage(User).create(User(
        id=user_id, username=username,
        password_hash=await hash_password(f"{username}pass1"),
        created_at=datetime.now(UTC), role="user",
    ))
    workspaces = world.sp.get_storage(Workspace)
    workspace = await workspaces.get("ws-task")
    assert workspace is not None
    await workspaces.update(workspace.model_copy(update={
        "access": Access(grants={user_id: access}),
    }))
    login = await world.client.post(
        "/v1/auth/login",
        json={"username": username, "password": f"{username}pass1"},
    )
    assert login.status_code == 200, login.text


async def _records(sp: SqliteStorageProvider) -> list[ToolApprovalRecord]:
    return list((await sp.get_storage(ToolApprovalRecord).list(
        OffsetPage(offset=0, length=100),
    )).items)


async def _make_mixed_park(
    sp: SqliteStorageProvider,
    task: ToolCallTask,
    *,
    classic_gate_id: str,
) -> None:
    sessions = sp.get_storage(WorkspaceSession)
    session = await sessions.get(task.session_id)
    assert session is not None
    classic_call = "classic-call"
    event_key = f"tool_approval:{task.session_id}:{classic_call}"
    metadata = {
        "gate_id": classic_gate_id,
        "approvers": None,
        "original_call": {
            "id": classic_call, "name": "classic", "arguments": {},
        },
    }
    await sessions.update(session.model_copy(update={
        "parked_event_key": event_key,
        "parked_state": {
            "tool_call_id": classic_call,
            "yielded": {
                "tool_name": "_approval", "event_key": event_key,
                "resume_metadata": metadata,
            },
            "graph_checkpoint": {
                "pending_toolcalls": [{
                    "node_id": "classic-node", "tool_call_id": classic_call,
                    "parked_event_key": event_key, "arguments": {},
                    "tool_name": "_approval", "resume_metadata": metadata,
                    "scoped_tool_call_id": None,
                }],
                "pending_agent_yields": [], "pending_dispatch": [],
                "pending_tool_waits": [{
                    "node_id": "task-node",
                    "outstanding_task_ids": [task.id],
                    "notifying_results": [],
                }],
            },
        },
    }))


@pytest.mark.asyncio
async def test_pending_projects_occurrence_and_approve_is_durable(world) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(sp)
    published: list[str] = []
    real_publish = world.app.state.event_bus.publish

    async def _publish_spy(event_key, payload=None):
        published.append(event_key)
        return await real_publish(event_key, payload)

    world.app.state.event_bus.publish = _publish_spy

    pending = await client.get("/v1/sessions/task-sid/tool_approval/pending")
    assert pending.status_code == 200, pending.text
    assert pending.json() == {
        "tool_call_id": "wire-1", "tool_name": "dangerous",
        "toolset_id": None, "arguments": {"path": "/tmp/x"},
        "policy_id": "policy-1", "approval_type": "required",
        "gate_reason": "matched", "approvers": None, "gate_id": TOKEN,
        "parked_at": pending.json()["parked_at"],
        "timeout_at": pending.json()["timeout_at"], "status": "pending",
    }

    response = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "decision": "approved"},
    )
    assert response.status_code == 202, response.text
    stored = await sp.get_storage(ToolCallTask).get(task.id)
    assert stored is not None and stored.state is ToolCallTaskState.QUEUED
    assert stored.gate_resolution["payload"]["decision"] == "approved"
    records = await sp.get_storage(ToolApprovalRecord).list(
        OffsetPage(offset=0, length=10)
    )
    assert [record.gate_event_key for record in records.items] == [
        f"tool_approval:task-sid:wire-1@{TOKEN}"
    ]
    assert await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert "tool_approval:task-sid:wire-1" not in published


@pytest.mark.asyncio
async def test_identical_retry_is_accepted_but_conflict_is_refused(world) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(sp)
    body = {"tool_call_id": "wire-1", "gate_id": TOKEN, "decision": "rejected", "reason": "no"}
    assert (await client.post("/v1/sessions/task-sid/tool_approval/respond", json=body)).status_code == 202
    await world.engine.delete_lease(ClaimKind.TOOL_CALL, task.id)
    assert not await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert (await client.post("/v1/sessions/task-sid/tool_approval/respond", json=body)).status_code == 202
    assert await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    conflict = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "decision": "approved"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["extensions"]["code"] == "already_decided"


@pytest.mark.asyncio
async def test_live_membership_and_raw_call_identity_are_required(world) -> None:
    sp, client = world.sp, world.client
    await _park_task_approval(sp)
    wrong_call = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={"tool_call_id": "other", "gate_id": TOKEN, "decision": "approved"},
    )
    assert wrong_call.status_code == 404

    sessions = sp.get_storage(WorkspaceSession)
    session = await sessions.get("task-sid")
    assert session is not None
    await sessions.update(session.model_copy(update={
        "parked_state": {
            "kind": "tool_wait", "outstanding_task_ids": [],
            "notifying_task_ids": [],
        },
    }))
    assert (
        await client.get("/v1/sessions/task-sid/tool_approval/pending")
    ).status_code == 404


@pytest.mark.asyncio
async def test_replaced_occurrence_is_stale(world) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(sp)
    old_gate = TaskGateState.from_jsonable(task.gate_state)
    new_token = "c" * 32
    new_gate = replace(old_gate, occurrence_token=new_token, gate_seq=2)
    await sp.get_storage(ToolCallTask).update(task.model_copy(update={
        "gate_occurrence_token": new_token,
        "gate_seq": 2,
        "gate_state": new_gate.to_jsonable(),
    }))

    response = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "decision": "approved"},
    )
    assert response.status_code == 409
    assert response.json()["extensions"]["code"] == "approval_stale"


@pytest.mark.asyncio
async def test_access_approver_and_engine_refusals_precede_mutation(world) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(
        sp, approvers={"kind": "users", "users": ["alice"]},
    )
    body = {
        "tool_call_id": "wire-1", "gate_id": TOKEN,
        "decision": "approved",
    }

    await _login_user(world, "reader", access="read")
    denied = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond", json=body,
    )
    assert denied.status_code == 403
    assert "needs full access" in denied.json()["detail"]

    world.app.state.claim_engine = None
    await _login_user(world, "bob", access="full")
    wrong_approver = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond", json=body,
    )
    assert wrong_approver.status_code == 403
    assert wrong_approver.json()["extensions"]["error"] == "approver_mismatch"

    await _login_user(world, "alice", access="full")
    missing_engine = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond", json=body,
    )
    assert missing_engine.status_code == 500
    assert missing_engine.json()["type"].endswith("/errors/internal")

    stored = await sp.get_storage(ToolCallTask).get(task.id)
    assert stored is not None and stored.state is ToolCallTaskState.GATED
    assert stored.gate_resolution is None
    assert not await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert await _records(sp) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parked_status", "session_status", "session_binding_epoch"),
    [
        (None, SessionStatus.RUNNING, 0),
        ("parked", SessionStatus.ENDED, 0),
        ("parked", SessionStatus.RUNNING, 1),
    ],
    ids=["unparked", "ended", "stale-binding"],
)
async def test_non_live_session_occurrence_is_hidden_and_cannot_move(
    world,
    parked_status,
    session_status,
    session_binding_epoch,
) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(
        sp, parked_status=parked_status, session_status=session_status,
        session_binding_epoch=session_binding_epoch,
    )
    before = (await sp.get_storage(ToolCallTask).get(task.id)).model_dump(mode="json")

    pending = await client.get("/v1/sessions/task-sid/tool_approval/pending")
    responded = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={
            "tool_call_id": "wire-1", "gate_id": TOKEN,
            "decision": "approved",
        },
    )

    assert pending.status_code == 404
    assert responded.status_code == 404
    after = await sp.get_storage(ToolCallTask).get(task.id)
    assert after is not None and after.model_dump(mode="json") == before
    assert not await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert await _records(sp) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["envelope", "top-level-mirror"])
async def test_corrupt_task_occurrence_is_not_exposed_or_decided(
    world, corruption,
) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(sp)
    changes = (
        {"gate_state": {"version": 999}}
        if corruption == "envelope"
        else {"gate_event_key": "tool_approval:other:event"}
    )
    await sp.get_storage(ToolCallTask).update(task.model_copy(update=changes))
    before = (await sp.get_storage(ToolCallTask).get(task.id)).model_dump(mode="json")

    assert (
        await client.get("/v1/sessions/task-sid/tool_approval/pending")
    ).status_code == 404
    response = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={
            "tool_call_id": "wire-1", "gate_id": TOKEN,
            "decision": "approved",
        },
    )
    assert response.status_code == 404
    after = await sp.get_storage(ToolCallTask).get(task.id)
    assert after is not None and after.model_dump(mode="json") == before
    assert not await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert await _records(sp) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_state", "claim_token", "delete_row"),
    [
        (ToolCallTaskState.RUNNING, "live-claim", False),
        (ToolCallTaskState.GATED, None, True),
    ],
    ids=["running", "missing"],
)
async def test_moved_or_missing_occurrence_returns_404_without_audit(
    world, task_state, claim_token, delete_row,
) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(
        sp, task_state=task_state, claim_token=claim_token,
    )
    if delete_row:
        await sp.get_storage(ToolCallTask).delete(task.id)
    response = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={
            "tool_call_id": "wire-1", "gate_id": TOKEN,
            "decision": "approved",
        },
    )
    assert response.status_code == 404
    assert not await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert await _records(sp) == []
    if not delete_row:
        stored = await sp.get_storage(ToolCallTask).get(task.id)
        assert stored is not None and stored.state is task_state
        assert stored.gate_resolution is None


@pytest.mark.asyncio
async def test_terminal_cas_race_returns_404_without_audit(
    world, monkeypatch,
) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(sp)

    async def _terminal_race(*args, **kwargs):
        return TaskGateResumeOutcome.REFUSED_TERMINAL

    monkeypatch.setattr(
        "primer.api.routers.tool_approval.durably_mark_tool_call_task_resumable",
        _terminal_race,
    )
    response = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={
            "tool_call_id": "wire-1", "gate_id": TOKEN,
            "decision": "approved",
        },
    )
    assert response.status_code == 404
    stored = await sp.get_storage(ToolCallTask).get(task.id)
    assert stored is not None and stored.state is ToolCallTaskState.GATED
    assert stored.gate_resolution is None
    assert not await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert await _records(sp) == []


@pytest.mark.asyncio
async def test_audit_storage_failure_is_best_effort(
    world, monkeypatch,
) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(sp)
    record_storage = sp.get_storage(ToolApprovalRecord)

    async def _fail_create(_record):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(record_storage, "create", _fail_create)
    response = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={
            "tool_call_id": "wire-1", "gate_id": TOKEN,
            "decision": "approved",
        },
    )
    assert response.status_code == 202, response.text
    stored = await sp.get_storage(ToolCallTask).get(task.id)
    assert stored is not None and stored.state is ToolCallTaskState.QUEUED
    assert await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert await _records(sp) == []


@pytest.mark.asyncio
async def test_mixed_park_routes_exact_classic_but_rejects_tokenless_collision(
    world,
) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(sp)
    classic_token = "b" * 32
    await _make_mixed_park(sp, task, classic_gate_id=classic_token)

    tokenless = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={"tool_call_id": "classic-call", "decision": "approved"},
    )
    assert tokenless.status_code == 404
    session = await sp.get_storage(WorkspaceSession).get("task-sid")
    assert session is not None and session.parked_status == "parked"
    stored = await sp.get_storage(ToolCallTask).get(task.id)
    assert stored is not None and stored.state is ToolCallTaskState.GATED

    classic = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={
            "tool_call_id": "classic-call", "gate_id": classic_token,
            "decision": "approved",
        },
    )
    assert classic.status_code == 202, classic.text
    stored = await sp.get_storage(ToolCallTask).get(task.id)
    assert stored is not None and stored.state is ToolCallTaskState.GATED


@pytest.mark.asyncio
async def test_corrupt_task_token_in_mixed_park_never_falls_to_classic(world) -> None:
    sp, client = world.sp, world.client
    task = await _park_task_approval(sp)
    await _make_mixed_park(sp, task, classic_gate_id=TOKEN)
    await sp.get_storage(ToolCallTask).update(task.model_copy(update={
        "gate_state": {"version": 999},
    }))

    response = await client.post(
        "/v1/sessions/task-sid/tool_approval/respond",
        json={
            "tool_call_id": "classic-call", "gate_id": TOKEN,
            "decision": "approved",
        },
    )
    assert response.status_code == 404
    session = await sp.get_storage(WorkspaceSession).get("task-sid")
    assert session is not None and session.parked_status == "parked"
    assert await _records(sp) == []


@pytest.mark.asyncio
async def test_classic_approval_route_is_unchanged(world) -> None:
    sp, client = world.sp, world.client
    await sp.get_storage(WorkspaceSession).create(
        _approval_session(
            session_id="classic-sid", tool_call_id="classic-call", gate_id="b" * 32,
        ).model_copy(update={"workspace_id": "ws-task"})
    )
    response = await client.post(
        "/v1/sessions/classic-sid/tool_approval/respond",
        json={
            "tool_call_id": "classic-call", "gate_id": "b" * 32,
            "decision": "approved",
        },
    )
    assert response.status_code == 202, response.text
