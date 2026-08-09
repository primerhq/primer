"""Steer-body external tool registration + tool_results dispatch rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.model.agent import Agent, AgentModel
from primer.model.external_tool import ExternalToolCall
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from tests.api.test_workspaces import _FakeBackend, _SP, _provider, _template

DEF = {
    "name": "lookup_customer",
    "description": "Look up a customer.",
    "schema": {"type": "object"},
}


@pytest.fixture
def sp() -> _SP:
    return _SP()


@pytest.fixture
def pr(sp) -> ProviderRegistry:
    return ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda t: object(),
    )


@pytest.fixture
def wsr(sp) -> WorkspaceRegistry:
    return WorkspaceRegistry(sp, factory=_FakeBackend)


@pytest.fixture
def app(sp, pr, wsr):
    return create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=pr,
        workspace_registry=wsr,
    )


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:
            pass
        yield c


async def _setup_ws(client, wsr) -> str:
    await client.post(
        "/v1/workspace_providers", json=_provider().model_dump(mode="json")
    )
    await client.post(
        "/v1/workspace_templates", json=_template().model_dump(mode="json")
    )
    post = await client.post("/v1/workspaces", json={"template_id": "tpl-1"})
    wid = post.json()["id"]
    backend = await wsr.get_backend("local-1")
    ws = await backend.get(wid)
    ws.add_session("sess-1", "agent-ext")
    return wid


async def _seed_agent(sp, *, allow: bool) -> None:
    await sp.get_storage(Agent).create(
        Agent(
            id="agent-ext",
            description="d",
            model=AgentModel(profile_id="prof-1"),
            allow_external_tools=allow,
        )
    )


async def _seed_session(sp, wid: str, **over) -> WorkspaceSession:
    now = datetime.now(UTC)
    row = WorkspaceSession(
        id="sess-1",
        workspace_id=wid,
        binding=AgentSessionBinding(agent_id="agent-ext"),
        status=SessionStatus.RUNNING,
        created_at=now,
        started_at=now,
        **over,
    )
    await sp.get_storage(WorkspaceSession).create(row)
    return row


def _parked_over(sid: str) -> dict:
    now = datetime.now(UTC)
    ek = f"external_tool:{sid}:tc-1"
    return dict(
        parked_status="parked",
        parked_event_key=ek,
        parked_until=now + timedelta(seconds=600),
        parked_at=now,
        parked_state={
            "schema_version": 1,
            "tool_call_id": "tc-1",
            "yielded": {
                "tool_name": "_external",
                "event_key": ek,
                "timeout": 600.0,
                "resume_metadata": {
                    "original_call": {
                        "id": "tc-1",
                        "name": "lookup_customer",
                        "arguments": {},
                    },
                    "external_call_row_id": "etool-fixed-1",
                    "parked_at_iso": now.isoformat(),
                },
            },
            "llm_messages": [],
            "turn_no": 1,
            "started_at": now.isoformat(),
            "resume_event_payload": None,
        },
    )


async def _seed_call(sp, sid: str) -> None:
    await sp.get_storage(ExternalToolCall).create(
        ExternalToolCall(
            id="etool-fixed-1",
            session_id=sid,
            tool_call_id="tc-1",
            tool_name="lookup_customer",
            arguments={},
            created_at=datetime.now(UTC),
        )
    )


@pytest.mark.asyncio
async def test_steer_rejects_external_tools_when_flag_off(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=False)
    await _seed_session(sp, wid)
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"instruction": "go", "external_tools": [DEF]},
    )
    assert r.status_code == 422, r.text
    assert "allow_external_tools" in r.text


@pytest.mark.asyncio
async def test_steer_stamps_defs_on_session_row(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_session(sp, wid)
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"instruction": "go", "external_tools": [DEF]},
    )
    assert r.status_code == 200, r.text
    row = await sp.get_storage(WorkspaceSession).get("sess-1")
    assert row.external_tools
    assert row.external_tools[0]["name"] == "lookup_customer"
    assert "schema" in row.external_tools[0]


@pytest.mark.asyncio
async def test_pure_tool_results_resumes_parked_call(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_session(sp, wid, **_parked_over("sess-1"))
    await _seed_call(sp, "sess-1")
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={
            "tool_results": [
                {"tool_call_id": "tc-1", "result": {"customer": "c1"}},
            ]
        },
    )
    assert r.status_code == 200, r.text
    row = await sp.get_storage(WorkspaceSession).get("sess-1")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {
        "result": {"customer": "c1"},
        "is_error": False,
    }
    stored = await sp.get_storage(ExternalToolCall).get("etool-fixed-1")
    assert stored.status == "completed"
    assert stored.resolved_at is not None


@pytest.mark.asyncio
async def test_unknown_tool_result_409s_atomically(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_session(sp, wid, **_parked_over("sess-1"))
    await _seed_call(sp, "sess-1")
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={
            "tool_results": [
                {"tool_call_id": "tc-1", "result": "ok"},
                {"tool_call_id": "tc-nope", "result": "?"},
            ]
        },
    )
    assert r.status_code == 409, r.text
    stored = await sp.get_storage(ExternalToolCall).get("etool-fixed-1")
    assert stored.status == "pending"  # nothing applied
    row = await sp.get_storage(WorkspaceSession).get("sess-1")
    assert row.parked_status == "parked"


@pytest.mark.asyncio
async def test_message_while_pending_cancels_with_synthetic_result(
    client, wsr, sp
):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_session(sp, wid, **_parked_over("sess-1"))
    await _seed_call(sp, "sess-1")
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer",
        json={"instruction": "actually, do something else"},
    )
    assert r.status_code == 200, r.text
    stored = await sp.get_storage(ExternalToolCall).get("etool-fixed-1")
    assert stored.status == "cancelled"
    assert stored.result["reason"] == "superseded by new user message"
    row = await sp.get_storage(WorkspaceSession).get("sess-1")
    # Park woken with the synthetic cancelled marker payload; the resumed
    # turn pairs the call before consuming the queued instruction.
    assert row.parked_status == "resumable"
    payload = row.parked_state["resume_event_payload"]
    assert payload["__yield_cancelled__"] is True
    assert payload["reason"] == "superseded by new user message"


@pytest.mark.asyncio
async def test_steer_requires_instruction_or_results(client, wsr, sp):
    wid = await _setup_ws(client, wsr)
    await _seed_agent(sp, allow=True)
    await _seed_session(sp, wid)
    r = await client.post(
        f"/v1/workspaces/{wid}/sessions/sess-1/steer", json={}
    )
    assert r.status_code == 422
