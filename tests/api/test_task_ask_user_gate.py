"""Dormant REST routing for task-scoped ``ask_user`` gates."""

from __future__ import annotations

import json
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
from primer.int.claim import ClaimKind
from primer.model.access import Access
from primer.model.common import dump_for_storage
from primer.model.provider import SqliteConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.user import User
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.task_gate_runtime import TaskGateState, gate_state_sha256
from tests.api.test_yields_durable_flip import _make_ask_user_parked_session


TOKEN = "a" * 32
EPOCH = "e" * 32


@pytest.fixture(autouse=True)
def _exercise_dormant_task_ask_router(request, monkeypatch) -> None:
    if "task_ask_capability_disabled" in request.fixturenames:
        return
    monkeypatch.setattr(
        "primer.api.routers.yields._task_ask_user_decisions_enabled",
        lambda _request: True,
    )


@pytest.fixture
def task_ask_capability_disabled() -> None:
    """Opt into the production S0 hard-off state."""


@dataclass
class World:
    sp: SqliteStorageProvider
    client: httpx.AsyncClient
    app: Any
    engine: InMemoryClaimEngine


@pytest_asyncio.fixture
async def world(tmp_path) -> AsyncIterator[World]:
    sp = SqliteStorageProvider(SqliteConfig(path=str(tmp_path / "task-ask.sqlite")))
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
        cross_encoder_factory=lambda row: object(),
        toolset_factory=lambda row: object(),
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


async def _park_task_ask(
    sp: SqliteStorageProvider,
    sid: str = "task-ask-sid",
    *,
    token: str = TOKEN,
    node: str = "x",
    scoped_turn_no: int = 0,
    response_schema: dict[str, Any] | None = None,
) -> ToolCallTask:
    now = datetime.now(UTC)
    task_id = f"{sid}/{node}:tool:{scoped_turn_no}:1"
    event_key = f"ask_user:{sid}:wire-1"
    gate = TaskGateState(
        occurrence_token=token,
        gate_seq=1,
        event_key=event_key,
        yielded=Yielded(
            tool_name="ask_user",
            event_key=event_key,
            resume_metadata={
                "prompt": "Pick a colour",
                "response_schema": response_schema,
            },
        ),
        tool_call_id="wire-1",
        tool_arguments={"prompt": "Pick a colour"},
        parked_at=now,
        deadline=now + timedelta(minutes=5),
        binding_epoch=0,
    )
    blob = gate.to_jsonable()
    task = ToolCallTask(
        id=task_id,
        session_id=sid,
        turn_no=0,
        tool_name="ask_user",
        state=ToolCallTaskState.GATED,
        record_seq=1,
        call_id="wire-1",
        gate_event_key=event_key,
        gate_until=gate.deadline,
        gate_occurrence_token=token,
        gate_seq=1,
        gate_state=blob,
        gate_state_sha256=gate_state_sha256(blob),
        batch_task_ids=[task_id],
        created_at=now,
    )
    await sp.get_storage(ToolCallTask).create(task)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id=sid,
        workspace_id="ws-task",
        binding=AgentSessionBinding(agent_id="agent-1"),
        status=SessionStatus.RUNNING,
        created_at=now,
        parked_status="parked",
        parked_at=now,
        parked_event_key=f"tool_wait:{sid}:x:tool:0:1",
        parked_state={
            "kind": "tool_wait",
            "outstanding_task_ids": [task_id],
            "notifying_task_ids": [],
        },
        responder_seq=0,
        responder_epoch=EPOCH,
        responder_reservation_version=1,
        responder_reservations={},
    ))
    return task


def _storage_bytes(row) -> bytes:
    return json.dumps(
        dump_for_storage(row), sort_keys=True, separators=(",", ":"),
    ).encode()


async def _add_task_ask_sibling(
    sp: SqliteStorageProvider,
    task: ToolCallTask,
    *,
    token: str,
    call_id: str,
    seq: int,
    created_at: datetime,
) -> ToolCallTask:
    """Add another production-shaped ask row to the same live tool wait."""
    task_id = f"{task.session_id}/x:tool:0:{seq}"
    event_key = f"ask_user:{task.session_id}:{call_id}"
    gate = TaskGateState(
        occurrence_token=token,
        gate_seq=1,
        event_key=event_key,
        yielded=Yielded(
            tool_name="ask_user",
            event_key=event_key,
            resume_metadata={"prompt": f"Prompt for {call_id}"},
        ),
        tool_call_id=call_id,
        tool_arguments={"prompt": f"Prompt for {call_id}"},
        parked_at=created_at,
        deadline=created_at + timedelta(minutes=5),
        binding_epoch=0,
    )
    blob = gate.to_jsonable()
    sibling = ToolCallTask(
        id=task_id,
        session_id=task.session_id,
        turn_no=0,
        tool_name="ask_user",
        state=ToolCallTaskState.GATED,
        record_seq=seq,
        call_id=call_id,
        gate_event_key=event_key,
        gate_until=gate.deadline,
        gate_occurrence_token=token,
        gate_seq=1,
        gate_state=blob,
        gate_state_sha256=gate_state_sha256(blob),
        batch_task_ids=[task_id],
        created_at=created_at,
    )
    await sp.get_storage(ToolCallTask).create(sibling)
    sessions = sp.get_storage(WorkspaceSession)
    session = await sessions.get(task.session_id)
    assert session is not None and session.parked_state is not None
    parked_state = dict(session.parked_state)
    parked_state["outstanding_task_ids"] = [
        *(parked_state.get("outstanding_task_ids") or []),
        sibling.id,
    ]
    await sessions.update(session.model_copy(update={"parked_state": parked_state}))
    return sibling


async def _login_user(world: World, username: str, *, access: str | None) -> None:
    user_id = f"user-{username}"
    await world.sp.get_storage(User).create(User(
        id=user_id,
        username=username,
        password_hash=await hash_password(f"{username}pass1"),
        created_at=datetime.now(UTC),
        role="user",
    ))
    if access is not None:
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


async def _make_mixed_park(sp: SqliteStorageProvider, task: ToolCallTask) -> None:
    sessions = sp.get_storage(WorkspaceSession)
    session = await sessions.get(task.session_id)
    assert session is not None
    classic_token = "c" * 32
    event_key = f"ask_user:{task.session_id}:classic-call"
    metadata = {
        "gate_id": classic_token,
        "prompt": "Classic question",
        "response_schema": None,
    }
    await sessions.update(session.model_copy(update={
        "parked_event_key": event_key,
        "parked_state": {
            "tool_call_id": "classic-call",
            "yielded": {
                "tool_name": "_approval",
                "event_key": event_key,
                "resume_metadata": metadata,
            },
            "graph_checkpoint": {
                "pending_agent_yields": [{
                    "node_id": "agent-node",
                    "tool_call_id": "classic-call",
                    "tool_name": "ask_user",
                    "event_key": event_key,
                    "resume_metadata": metadata,
                }],
                "pending_dispatch": [],
                "pending_toolcalls": [],
                "pending_tool_waits": [{
                    "node_id": "task-node",
                    "outstanding_task_ids": [task.id],
                    "notifying_results": [],
                }],
            },
        },
    }))


@pytest.mark.asyncio
async def test_s0_hides_seeded_task_ask_without_mutation(
    world, task_ask_capability_disabled,
) -> None:
    task = await _park_task_ask(world.sp, "s0-task-ask")
    before = await world.sp.get_storage(ToolCallTask).get(task.id)

    pending = await world.client.get(
        "/v1/sessions/s0-task-ask/ask_user/pending",
    )
    responded = await world.client.post(
        "/v1/sessions/s0-task-ask/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )

    assert pending.status_code == 404
    assert responded.status_code == 404
    assert await world.sp.get_storage(ToolCallTask).get(task.id) == before
    assert not await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)


@pytest.mark.asyncio
async def test_pending_and_respond_project_existing_shapes_without_bus_publish(
    world,
) -> None:
    task = await _park_task_ask(world.sp)
    published: list[str] = []

    async def _publish_spy(event_key, payload=None):
        published.append(event_key)

    world.app.state.event_bus.publish = _publish_spy
    pending = await world.client.get(
        "/v1/sessions/task-ask-sid/ask_user/pending",
    )
    assert pending.status_code == 200, pending.text
    assert pending.json() == {
        "tool_call_id": "wire-1",
        "prompt": "Pick a colour",
        "response_schema": None,
        "gate_id": TOKEN,
        "parked_at": pending.json()["parked_at"],
    }

    response = await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={
            "tool_call_id": "wire-1", "gate_id": TOKEN,
            "response": "blue", "person": {"id": "attacker"},
        },
    )
    assert response.status_code == 202, response.text
    stored = await world.sp.get_storage(ToolCallTask).get(task.id)
    session = await world.sp.get_storage(WorkspaceSession).get("task-ask-sid")
    assert stored is not None and stored.state is ToolCallTaskState.QUEUED
    assert stored.gate_resolution["payload"] == {"response": "blue"}
    assert session is not None and session.turn_person is not None
    assert session.turn_person.display == "admin"
    assert session.turn_person_source_key in session.responder_reservations
    assert await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert published == []


@pytest.mark.asyncio
async def test_pending_orders_two_task_asks_by_created_at_then_id(world) -> None:
    later = await _park_task_ask(world.sp, "ordered-task-asks")
    earlier = await _add_task_ask_sibling(
        world.sp,
        later,
        token="b" * 32,
        call_id="wire-2",
        seq=2,
        created_at=later.created_at - timedelta(seconds=1),
    )

    first = await world.client.get(
        "/v1/sessions/ordered-task-asks/ask_user/pending",
    )
    second = await world.client.get(
        "/v1/sessions/ordered-task-asks/ask_user/pending",
    )

    assert first.status_code == 200
    assert first.json()["tool_call_id"] == earlier.call_id
    assert first.json()["gate_id"] == earlier.gate_occurrence_token
    assert second.json() == first.json()


@pytest.mark.asyncio
async def test_duplicate_occurrence_token_is_corrupt_without_mutation(world) -> None:
    task = await _park_task_ask(world.sp, "duplicate-task-ask")
    sibling = await _add_task_ask_sibling(
        world.sp,
        task,
        token=TOKEN,
        call_id="wire-2",
        seq=2,
        created_at=task.created_at + timedelta(seconds=1),
    )
    tasks = world.sp.get_storage(ToolCallTask)
    sessions = world.sp.get_storage(WorkspaceSession)
    task_before = await tasks.get(task.id)
    sibling_before = await tasks.get(sibling.id)
    session_before = await sessions.get(task.session_id)

    response = await world.client.post(
        "/v1/sessions/duplicate-task-ask/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )

    assert response.status_code == 404
    assert response.json()["extensions"]["code"] == "gate_not_pending"
    assert await tasks.get(task.id) == task_before
    assert await tasks.get(sibling.id) == sibling_before
    assert await sessions.get(task.session_id) == session_before
    assert not await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert not await world.engine.has_lease(ClaimKind.TOOL_CALL, sibling.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["stale_task_turn", "scoped_turn_mismatch"])
async def test_turn_identity_mismatch_get_and_post_leave_canonical_rows_unchanged(
    world, case,
) -> None:
    task = await _park_task_ask(
        world.sp,
        f"turn-mismatch-{case}",
        scoped_turn_no=1 if case == "scoped_turn_mismatch" else 0,
    )
    tasks = world.sp.get_storage(ToolCallTask)
    sessions = world.sp.get_storage(WorkspaceSession)
    if case == "stale_task_turn":
        session = await sessions.get(task.session_id)
        assert session is not None
        await sessions.update(session.model_copy(update={"turn_no": 1}))
    task_before = await tasks.get(task.id)
    session_before = await sessions.get(task.session_id)
    assert task_before is not None and session_before is not None
    task_bytes = _storage_bytes(task_before)
    session_bytes = _storage_bytes(session_before)
    lease_before = await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)

    pending = await world.client.get(
        f"/v1/sessions/{task.session_id}/ask_user/pending",
    )

    assert pending.status_code == 404
    task_after_get = await tasks.get(task.id)
    session_after_get = await sessions.get(task.session_id)
    assert task_after_get is not None and session_after_get is not None
    assert _storage_bytes(task_after_get) == task_bytes
    assert _storage_bytes(session_after_get) == session_bytes
    assert await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id) is lease_before

    responded = await world.client.post(
        f"/v1/sessions/{task.session_id}/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )

    assert responded.status_code == 404
    assert responded.json()["extensions"]["code"] == "gate_not_pending"
    task_after_post = await tasks.get(task.id)
    session_after_post = await sessions.get(task.session_id)
    assert task_after_post is not None and session_after_post is not None
    assert _storage_bytes(task_after_post) == task_bytes
    assert _storage_bytes(session_after_post) == session_bytes
    assert await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id) is lease_before


@pytest.mark.asyncio
async def test_schema_refusal_precedes_any_task_or_session_mutation(world) -> None:
    task = await _park_task_ask(
        world.sp, response_schema={"type": "integer"},
    )
    task_before = await world.sp.get_storage(ToolCallTask).get(task.id)
    session_before = await world.sp.get_storage(WorkspaceSession).get(task.session_id)

    response = await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )

    assert response.status_code == 422
    assert await world.sp.get_storage(ToolCallTask).get(task.id) == task_before
    assert await world.sp.get_storage(WorkspaceSession).get(task.session_id) == session_before


@pytest.mark.asyncio
async def test_identical_retry_repairs_lease_and_conflict_is_409(world) -> None:
    task = await _park_task_ask(world.sp)
    body = {"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"}
    assert (await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond", json=body,
    )).status_code == 202
    session_before = await world.sp.get_storage(WorkspaceSession).get(task.session_id)
    await world.engine.delete_lease(ClaimKind.TOOL_CALL, task.id)
    assert (await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond", json=body,
    )).status_code == 202
    assert await world.engine.has_lease(ClaimKind.TOOL_CALL, task.id)
    assert await world.sp.get_storage(WorkspaceSession).get(task.session_id) == session_before

    conflict = await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "red"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["extensions"]["code"] == "already_decided"


@pytest.mark.asyncio
async def test_stale_replacement_and_graph_task_are_not_pending(world) -> None:
    task = await _park_task_ask(world.sp)
    gate = TaskGateState.from_jsonable(task.gate_state)
    replacement = replace(gate, occurrence_token="b" * 32, gate_seq=2)
    replacement_blob = replacement.to_jsonable()
    await world.sp.get_storage(ToolCallTask).update(task.model_copy(update={
        "gate_occurrence_token": replacement.occurrence_token,
        "gate_seq": replacement.gate_seq,
        "gate_state": replacement_blob,
        "gate_state_sha256": gate_state_sha256(replacement_blob),
    }))
    stale = await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )
    assert stale.status_code == 409
    assert stale.json()["extensions"]["code"] == "approval_stale"

    graph = await _park_task_ask(world.sp, "graph-task-ask", node="node-a")
    refused = await world.client.post(
        "/v1/sessions/graph-task-ask/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )
    assert refused.status_code == 404
    assert (await world.sp.get_storage(ToolCallTask).get(graph.id)).gate_resolution is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["running", "terminal", "missing", "corrupt"])
async def test_moved_terminal_missing_and_corrupt_task_asks_return_404(
    world, case,
) -> None:
    task = await _park_task_ask(world.sp)
    tasks = world.sp.get_storage(ToolCallTask)
    if case == "running":
        await tasks.update(task.model_copy(update={
            "state": ToolCallTaskState.RUNNING,
            "claim_token": "live-claim",
        }))
    elif case == "terminal":
        await tasks.update(task.model_copy(update={
            "state": ToolCallTaskState.DONE,
        }))
    elif case == "missing":
        await tasks.delete(task.id)
    else:
        await tasks.update(task.model_copy(update={"gate_state": {"version": 999}}))

    response = await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )

    assert response.status_code == 404
    stored = await tasks.get(task.id)
    if stored is not None:
        assert stored.gate_resolution is None


@pytest.mark.asyncio
async def test_read_full_and_missing_engine_refuse_before_mutation(world) -> None:
    task = await _park_task_ask(world.sp)
    await _login_user(world, "reader", access="read")
    assert (await world.client.get(
        "/v1/sessions/task-ask-sid/ask_user/pending",
    )).status_code == 200
    denied = await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )
    assert denied.status_code == 403

    await _login_user(world, "full", access="full")
    world.app.state.claim_engine = None
    missing = await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )
    assert missing.status_code == 500
    stored = await world.sp.get_storage(ToolCallTask).get(task.id)
    session = await world.sp.get_storage(WorkspaceSession).get(task.session_id)
    assert stored is not None and stored.gate_resolution is None
    assert session is not None and session.responder_seq == 0


@pytest.mark.asyncio
async def test_hidden_access_and_role_revoke_before_stage_a_leave_rows_unchanged(
    world, monkeypatch,
) -> None:
    task = await _park_task_ask(world.sp)
    task_before = await world.sp.get_storage(ToolCallTask).get(task.id)
    session_before = await world.sp.get_storage(WorkspaceSession).get(task.session_id)
    await _login_user(world, "hidden", access=None)
    assert (await world.client.get(
        "/v1/sessions/task-ask-sid/ask_user/pending",
    )).status_code == 404
    assert (await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )).status_code == 404

    await _login_user(world, "revoked", access="full")
    from primer.api.routers import yields as yields_router_module

    original_validate = yields_router_module._validate_response_against_schema

    async def _validate_then_revoke(*, response, schema):
        await original_validate(response=response, schema=schema)
        workspaces = world.sp.get_storage(Workspace)
        workspace = await workspaces.get("ws-task")
        assert workspace is not None
        await workspaces.update(workspace.model_copy(update={"access": Access()}))

    monkeypatch.setattr(
        yields_router_module, "_validate_response_against_schema",
        _validate_then_revoke,
    )
    revoked = await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={"tool_call_id": "wire-1", "gate_id": TOKEN, "response": "blue"},
    )
    assert revoked.status_code == 404
    assert await world.sp.get_storage(ToolCallTask).get(task.id) == task_before
    assert await world.sp.get_storage(WorkspaceSession).get(task.session_id) == session_before


@pytest.mark.asyncio
async def test_mixed_task_park_never_falls_through_to_tokenless_classic(
    world,
) -> None:
    task = await _park_task_ask(world.sp)
    await _make_mixed_park(world.sp, task)
    task_before = await world.sp.get_storage(ToolCallTask).get(task.id)
    session_before = await world.sp.get_storage(WorkspaceSession).get(task.session_id)

    response = await world.client.post(
        "/v1/sessions/task-ask-sid/ask_user/respond",
        json={"tool_call_id": "classic-call", "response": "classic answer"},
    )

    assert response.status_code == 404
    assert await world.sp.get_storage(ToolCallTask).get(task.id) == task_before
    assert await world.sp.get_storage(WorkspaceSession).get(task.session_id) == session_before


@pytest.mark.asyncio
async def test_tokenless_classic_only_stays_compatible_with_dormant_branch(world) -> None:
    session = _make_ask_user_parked_session(
        session_id="classic-ask", tool_call_id="classic-call",
    ).model_copy(update={"workspace_id": "ws-task"})
    await world.sp.get_storage(WorkspaceSession).create(session)

    response = await world.client.post(
        "/v1/sessions/classic-ask/ask_user/respond",
        json={"tool_call_id": "classic-call", "response": "blue"},
    )

    assert response.status_code == 202, response.text
    stored = await world.sp.get_storage(WorkspaceSession).get("classic-ask")
    assert stored is not None and stored.parked_status == "resumable"


@pytest.mark.asyncio
async def test_dormant_task_branch_does_not_change_public_ask_user_schemas(
    world,
) -> None:
    schemas = world.app.openapi()["components"]["schemas"]

    assert set(schemas["AskUserPendingResponse"]["properties"]) == {
        "tool_call_id", "prompt", "response_schema", "gate_id", "parked_at",
    }
    assert set(schemas["AskUserRespondBody"]["properties"]) == {
        "tool_call_id", "gate_id", "response",
    }
    assert not any("TaskAsk" in name for name in schemas)
