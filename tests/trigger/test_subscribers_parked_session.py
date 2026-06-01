"""parked_session dispatcher — Spec §5.4, Plan §5.4."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.trigger import ParkedSessionSubConfig, Subscription
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.trigger.subscribers import DispatchDeps
from primer.trigger.subscribers.parked_session import ParkedSessionDispatcher


def _make_sub(session_id: str, tool_call_id: str) -> Subscription:
    return Subscription(
        id="sb-1",
        trigger_id="tr-1",
        config=ParkedSessionSubConfig(
            session_id=session_id,
            tool_call_id=tool_call_id,
            parked_at=datetime.now(timezone.utc),
        ),
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )


def _parked_session(
    *,
    session_id: str = "se-parked",
    tool_call_id: str = "tc-1",
    workspace_id: str,
    agent_id: str,
) -> WorkspaceSession:
    event_key = f"subscribe_to_trigger:{tool_call_id}"
    return WorkspaceSession(
        id=session_id,
        workspace_id=workspace_id,
        binding=AgentSessionBinding(agent_id=agent_id),
        status=SessionStatus.WAITING,
        turn_status="idle",
        parked_status="parked",
        parked_event_key=event_key,
        parked_state={
            "tool_call_id": tool_call_id,
            "yielded": {
                "tool_name": "subscribe_to_trigger",
                "event_key": event_key,
                "resume_metadata": {"tool_call_id": tool_call_id},
            },
        },
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_dispatch_resumes_parked_session(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_event_bus, seeded_workspace, seeded_agent,
):
    """Happy path — payload + fire context land on the parked event_key."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(
        _parked_session(
            workspace_id=seeded_workspace.id, agent_id=seeded_agent.id,
        ),
    )
    # Seed the subscription row so the dispatcher can delete it.
    subs = fake_storage_provider.get_storage(Subscription)
    sub = _make_sub("se-parked", "tc-1")
    await subs.create(sub)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        event_bus=fake_event_bus,
    )
    res = await ParkedSessionDispatcher().dispatch(
        sub,
        rendered_payload='{"answer": 42}',
        fire_context={
            "trigger_id": "tr-1",
            "fired_at": "2026-06-01T09:00:00+00:00",
        },
        fire_id="fire-tr-1-300",
        deps=deps,
    )
    assert res.ok and not res.skipped
    assert res.artefact_id == "se-parked"

    # Event bus saw the publish on the parked event_key with the full
    # tool result envelope (ok + fire_context + parsed payload).
    assert len(fake_event_bus.published) == 1
    key, payload = fake_event_bus.published[0]
    assert key == "subscribe_to_trigger:tc-1"
    assert payload == {
        "ok": True,
        "fire_context": {
            "trigger_id": "tr-1",
            "fired_at": "2026-06-01T09:00:00+00:00",
        },
        "payload": {"answer": 42},
    }

    # Subscription was consumed (one-shot).
    assert await subs.get("sb-1") is None


@pytest.mark.asyncio
async def test_dispatch_passes_raw_payload_when_not_json(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_event_bus, seeded_workspace, seeded_agent,
):
    """Non-JSON rendered payload is forwarded verbatim under ``payload``."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    await sessions.create(
        _parked_session(
            workspace_id=seeded_workspace.id, agent_id=seeded_agent.id,
        ),
    )
    subs = fake_storage_provider.get_storage(Subscription)
    sub = _make_sub("se-parked", "tc-1")
    await subs.create(sub)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        event_bus=fake_event_bus,
    )
    res = await ParkedSessionDispatcher().dispatch(
        sub,
        rendered_payload="hello world",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-301",
        deps=deps,
    )
    assert res.ok
    _, payload = fake_event_bus.published[0]
    assert payload["payload"] == "hello world"


@pytest.mark.asyncio
async def test_dispatch_skips_when_session_unparked(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_event_bus, seeded_workspace, seeded_agent,
):
    """Running (unparked) session → skip + delete sub, no bus publish."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    running = WorkspaceSession(
        id="se-running",
        workspace_id=seeded_workspace.id,
        binding=AgentSessionBinding(agent_id=seeded_agent.id),
        status=SessionStatus.RUNNING,
        turn_status="running",
        parked_status=None,
        created_at=datetime.now(timezone.utc),
    )
    await sessions.create(running)
    subs = fake_storage_provider.get_storage(Subscription)
    sub = _make_sub("se-running", "tc-1")
    await subs.create(sub)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        event_bus=fake_event_bus,
    )
    res = await ParkedSessionDispatcher().dispatch(
        sub,
        rendered_payload="{}",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-400",
        deps=deps,
    )
    assert res.ok and res.skipped
    assert res.error_code == "skipped_session_unparked"
    assert fake_event_bus.published == []
    # Orphan sub was cleaned up.
    assert await subs.get("sb-1") is None


@pytest.mark.asyncio
async def test_dispatch_skips_when_session_missing(
    fake_storage_provider, fake_claim_engine, fake_scheduler, fake_event_bus,
):
    """Session was deleted between subscribe + fire — drop the sub, skip."""
    subs = fake_storage_provider.get_storage(Subscription)
    sub = _make_sub("se-gone", "tc-1")
    await subs.create(sub)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        event_bus=fake_event_bus,
    )
    res = await ParkedSessionDispatcher().dispatch(
        sub,
        rendered_payload="{}",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-401",
        deps=deps,
    )
    assert res.ok and res.skipped
    assert res.error_code == "skipped_session_unparked"
    assert await subs.get("sb-1") is None


@pytest.mark.asyncio
async def test_dispatch_skips_on_tool_call_id_mismatch(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_event_bus, seeded_workspace, seeded_agent,
):
    """Session parked on a different tool_call_id → skip + delete sub."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    # Park is on tc-1; sub points at tc-other.
    await sessions.create(
        _parked_session(
            workspace_id=seeded_workspace.id, agent_id=seeded_agent.id,
            tool_call_id="tc-1",
        ),
    )
    subs = fake_storage_provider.get_storage(Subscription)
    sub = _make_sub("se-parked", "tc-other")
    await subs.create(sub)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        event_bus=fake_event_bus,
    )
    res = await ParkedSessionDispatcher().dispatch(
        sub,
        rendered_payload="{}",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-402",
        deps=deps,
    )
    assert res.ok and res.skipped
    assert res.error_code == "skipped_session_unparked"
    assert fake_event_bus.published == []
    assert await subs.get("sb-1") is None
