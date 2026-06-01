"""fresh_session dispatchers — Spec §5.2, §5.3, Plan §5.3."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.trigger import (
    AgentFreshSubConfig,
    GraphFreshSubConfig,
    Subscription,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    GraphSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.trigger.subscribers import DispatchDeps
from primer.trigger.subscribers.agent_fresh_session import (
    AgentFreshSessionDispatcher,
)
from primer.trigger.subscribers.graph_fresh_session import (
    GraphFreshSessionDispatcher,
)


def _agent_sub(workspace_id, agent_id, parallelism="skip") -> Subscription:
    return Subscription(
        id="sb-1",
        trigger_id="tr-1",
        config=AgentFreshSubConfig(
            workspace_id=workspace_id, agent_id=agent_id,
        ),
        parallelism=parallelism,
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )


def _graph_sub(workspace_id, graph_id, parallelism="queue") -> Subscription:
    return Subscription(
        id="sb-1",
        trigger_id="tr-1",
        config=GraphFreshSubConfig(
            workspace_id=workspace_id, graph_id=graph_id,
        ),
        parallelism=parallelism,
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# agent_fresh_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_fresh_creates_session(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_agent,
):
    """Happy path — fresh session, agent binding, payload as initial instructions."""
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    res = await AgentFreshSessionDispatcher().dispatch(
        _agent_sub(seeded_workspace.id, seeded_agent.id),
        rendered_payload="run a check",
        fire_context={
            "trigger_id": "tr-1",
            "fired_at": "2026-06-01T09:00:00+00:00",
        },
        fire_id="fire-tr-1-100",
        deps=deps,
    )
    assert res.ok and not res.skipped
    assert res.artefact_id is not None

    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    sess = await sessions.get(res.artefact_id)
    assert sess is not None
    assert isinstance(sess.binding, AgentSessionBinding)
    assert sess.binding.agent_id == seeded_agent.id
    assert sess.workspace_id == seeded_workspace.id
    assert sess.initial_instructions == "run a check"
    assert sess.status == SessionStatus.RUNNING
    assert sess.metadata.get("subscription_id") == "sb-1"
    assert sess.metadata.get("trigger_id") == "tr-1"
    assert sess.metadata.get("fire_id") == "fire-tr-1-100"
    assert sess.metadata.get("fired_at") == "2026-06-01T09:00:00+00:00"


@pytest.mark.asyncio
async def test_agent_fresh_skips_when_subscription_busy(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_agent,
):
    """``skip`` + a still-running prior session → no new session, no error."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    existing = WorkspaceSession(
        id="se-existing",
        workspace_id=seeded_workspace.id,
        binding=AgentSessionBinding(agent_id=seeded_agent.id),
        status=SessionStatus.RUNNING,
        turn_status="running",
        metadata={"subscription_id": "sb-1"},
        created_at=datetime.now(timezone.utc),
    )
    await sessions.create(existing)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    res = await AgentFreshSessionDispatcher().dispatch(
        _agent_sub(seeded_workspace.id, seeded_agent.id, parallelism="skip"),
        rendered_payload="check",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-101",
        deps=deps,
    )
    assert res.ok and res.skipped
    assert res.error_code == "skipped_subscription_busy"

    # Only the pre-seeded row exists; no new session landed.
    all_sessions = list(sessions._data.values())  # noqa: SLF001
    assert {s.id for s in all_sessions} == {"se-existing"}


@pytest.mark.asyncio
async def test_agent_fresh_queue_creates_even_when_busy(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_agent,
):
    """``queue`` still creates a fresh session when a prior one is in flight."""
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    existing = WorkspaceSession(
        id="se-existing",
        workspace_id=seeded_workspace.id,
        binding=AgentSessionBinding(agent_id=seeded_agent.id),
        status=SessionStatus.RUNNING,
        turn_status="running",
        metadata={"subscription_id": "sb-1"},
        created_at=datetime.now(timezone.utc),
    )
    await sessions.create(existing)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    res = await AgentFreshSessionDispatcher().dispatch(
        _agent_sub(seeded_workspace.id, seeded_agent.id, parallelism="queue"),
        rendered_payload="check",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-102",
        deps=deps,
    )
    assert res.ok and not res.skipped
    assert res.artefact_id != "se-existing"


# ---------------------------------------------------------------------------
# graph_fresh_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_fresh_parses_payload_as_json(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_graph,
):
    """Happy path — payload parses as JSON, lands in metadata.graph_input."""
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    res = await GraphFreshSessionDispatcher().dispatch(
        _graph_sub(seeded_workspace.id, seeded_graph.id),
        rendered_payload=json.dumps({"tenant_id": "acme"}),
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-200",
        deps=deps,
    )
    assert res.ok
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    sess = await sessions.get(res.artefact_id)
    assert isinstance(sess.binding, GraphSessionBinding)
    assert sess.binding.graph_id == seeded_graph.id
    # The factory folds graph_input onto metadata for GraphSessionBinding;
    # the dispatcher also stamps it directly so both routes carry it.
    assert sess.metadata.get("graph_input") == {"tenant_id": "acme"}


@pytest.mark.asyncio
async def test_graph_fresh_invalid_json_payload(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_graph,
):
    """Non-JSON payload → structured ``graph_input_invalid`` error, no session."""
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    res = await GraphFreshSessionDispatcher().dispatch(
        _graph_sub(seeded_workspace.id, seeded_graph.id),
        rendered_payload="not json",
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-201",
        deps=deps,
    )
    assert res.ok is False
    assert res.error_code == "graph_input_invalid"
    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    assert list(sessions._data.values()) == []  # noqa: SLF001


@pytest.mark.asyncio
async def test_graph_fresh_rejects_non_object_payload(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_graph,
):
    """A JSON scalar/array doesn't qualify as graph_input either."""
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    res = await GraphFreshSessionDispatcher().dispatch(
        _graph_sub(seeded_workspace.id, seeded_graph.id),
        rendered_payload='"a string"',
        fire_context={"trigger_id": "tr-1"},
        fire_id="fire-tr-1-202",
        deps=deps,
    )
    assert res.ok is False
    assert res.error_code == "graph_input_invalid"
