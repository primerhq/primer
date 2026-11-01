"""The yield cancel is a durable wake that never overrides a decided gate (Part 3 Task 109, GS-6).

Characterization (pinned before the change, with the bus listener running): a cancel of a pending ask_user or approval answers
202 and the session resumes with today's cancelled payload. Then, inverted from the coverage-gap probe: with NO listener the
cancel still flips the row. A cancel of a park that already holds a decision (answered, approved, resumable) answers 202, as the
API always has, and changes nothing: the park keeps its decision and an ``_external`` audit row is not written.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from primer.api.app import create_test_app
from primer.bus.in_memory import InMemoryEventBus
from primer.bus.listener import YieldEventListener
from primer.model.external_tool import ExternalToolCall
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession


def _parked(session_id: str, tool_name: str, *, meta: dict | None = None) -> WorkspaceSession:
    now = datetime.now(UTC)
    kind = {"ask_user": "ask_user", "_approval": "tool_approval", "_external": "external_tool"}[tool_name]
    key = f"{kind}:{session_id}:tc-1"
    return WorkspaceSession(
        id=session_id, workspace_id="ws-x", binding=AgentSessionBinding(kind="agent", agent_id="ag-x"),
        status=SessionStatus.RUNNING, created_at=now,
        parked_status="parked", parked_event_key=key, parked_until=now + timedelta(seconds=600), parked_at=now,
        parked_state={
            "schema_version": 1, "tool_call_id": "tc-1",
            "yielded": {"tool_name": tool_name, "event_key": key, "timeout": 600.0,
                        "resume_metadata": {"original_call": {"id": "tc-1", "name": "t", "arguments": {}}, **(meta or {})}},
            "llm_messages": [], "turn_no": 1, "started_at": now.isoformat(), "resume_event_payload": None,
        },
    )


async def _run(fake_storage_provider, fake_provider_registry, sessions, calls, *, listener: bool):
    """Seed ``sessions``, then POST each ``(path, body)`` in ``calls``; answers the responses and the session store."""
    app = create_test_app(storage_provider=fake_storage_provider, provider_registry=fake_provider_registry)
    bus = InMemoryEventBus()
    await bus.initialize()
    app.state.event_bus = bus
    store = fake_storage_provider.get_storage(WorkspaceSession)
    for sess in sessions:
        await store.create(sess)
    lst = YieldEventListener(bus=bus, session_storage=store, engine=None) if listener else None
    if lst is not None:
        lst.start()
    responses = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/v1/auth/register", json={"username": "admin", "password": "testpassword"})
            for path, body in calls:
                responses.append(await c.post(path, json=body))
        for _ in range(50):
            if lst is None or all([(await store.get(s.id)).parked_status == "resumable" for s in sessions]):
                break
            await asyncio.sleep(0.02)
    finally:
        if lst is not None:
            await lst.stop()
        await bus.aclose()
    return responses, store


def _cancelled(payload: dict) -> dict:
    datetime.fromisoformat(payload["cancelled_at"])
    return {k: v for k, v in payload.items() if k != "cancelled_at"}


# --------------------------------------------------------------------------- characterization


@pytest.mark.parametrize("tool_name", ["ask_user", "_approval"])
async def test_a_cancel_answers_202_and_resumes_with_the_cancelled_payload(
    fake_storage_provider, fake_provider_registry, tool_name: str,
) -> None:
    responses, store = await _run(
        fake_storage_provider, fake_provider_registry, [_parked("s-1", tool_name)],
        [("/v1/sessions/s-1/yields/tc-1/cancel", {"reason": "skip"})], listener=True,
    )
    assert [r.status_code for r in responses] == [202]
    assert responses[0].json() == {"status": "accepted"}
    row = await store.get("s-1")
    assert row.parked_status == "resumable"
    assert _cancelled(row.parked_state["resume_event_payload"]) == {"__yield_cancelled__": True, "reason": "skip"}


# --------------------------------------------------------------------------- GS-6


@pytest.mark.parametrize("tool_name", ["ask_user", "_approval"])
async def test_a_cancel_lands_without_a_listener(fake_storage_provider, fake_provider_registry, tool_name: str) -> None:
    responses, store = await _run(
        fake_storage_provider, fake_provider_registry, [_parked("s-1", tool_name)],
        [("/v1/sessions/s-1/yields/tc-1/cancel", {"reason": "skip"})], listener=False,
    )
    assert [r.status_code for r in responses] == [202]
    row = await store.get("s-1")
    assert row.parked_status == "resumable"
    assert _cancelled(row.parked_state["resume_event_payload"]) == {"__yield_cancelled__": True, "reason": "skip"}


async def test_a_cancel_after_an_approve_answers_202_and_changes_nothing(fake_storage_provider, fake_provider_registry) -> None:
    responses, store = await _run(
        fake_storage_provider, fake_provider_registry, [_parked("s-1", "_approval")],
        [("/v1/sessions/s-1/tool_approval/respond", {"tool_call_id": "tc-1", "decision": "approved"}),
         ("/v1/sessions/s-1/yields/tc-1/cancel", {"reason": "skip"})],
        listener=False,
    )
    assert [r.status_code for r in responses] == [202, 202], [r.text for r in responses]
    row = await store.get("s-1")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"]["decision"] == "approved"


async def test_a_cancel_after_an_answer_answers_202_and_changes_nothing(fake_storage_provider, fake_provider_registry) -> None:
    responses, store = await _run(
        fake_storage_provider, fake_provider_registry, [_parked("s-1", "ask_user")],
        [("/v1/sessions/s-1/ask_user/respond", {"tool_call_id": "tc-1", "response": "bob"}),
         ("/v1/sessions/s-1/yields/tc-1/cancel", {"reason": "skip"})],
        listener=False,
    )
    assert [r.status_code for r in responses] == [202, 202], [r.text for r in responses]
    row = await store.get("s-1")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"]["response"] == "bob"


async def test_a_cancel_of_an_already_resumable_park_answers_202_and_leaves_park_and_row_unchanged(
    fake_storage_provider, fake_provider_registry,
) -> None:
    """Mirrors e2e T0801: the park is ALREADY ``resumable`` (decided, waiting for the worker) when the cancel arrives. The cancel
    answers 202 with no double flip: the park keeps its decision and the external call's audit row stays as it was."""
    sess = _parked("s-1", "_external", meta={"external_call_row_id": "etool-1"})
    sess.parked_status = "resumable"
    sess.parked_state["resume_event_payload"] = {"result": "done", "is_error": False}
    await fake_storage_provider.get_storage(ExternalToolCall).create(ExternalToolCall(
        id="etool-1", session_id="s-1", tool_call_id="tc-1", tool_name="t", arguments={}, created_at=datetime.now(UTC),
    ))
    responses, store = await _run(
        fake_storage_provider, fake_provider_registry, [sess],
        [("/v1/sessions/s-1/yields/tc-1/cancel", {"reason": "post-resumable cancel"})], listener=False,
    )
    assert [r.status_code for r in responses] == [202], responses[0].text
    assert responses[0].json() == {"status": "accepted"}
    row = await store.get("s-1")
    assert row.parked_status == "resumable"
    assert row.parked_state["resume_event_payload"] == {"result": "done", "is_error": False}
    call = await fake_storage_provider.get_storage(ExternalToolCall).get("etool-1")
    assert (call.status, call.result) == ("pending", None)
