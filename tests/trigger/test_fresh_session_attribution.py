"""Trigger fresh-session attribution — Layer 3 Task 2 (spec §8.1, §8.3).

``agent_fresh_session`` / ``graph_fresh_session`` subscriptions fire
sessions on the trigger's own behalf; the created row's
``initiated_by`` must reflect the trigger, not a human or the system
fallback, so a resumed/audited run is traceable to the subscription
that spawned it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from primer.model.trigger import (
    AgentFreshSubConfig,
    GraphFreshSubConfig,
    Subscription,
)
from primer.model.workspace_session import WorkspaceSession
from primer.trigger.subscribers import DispatchDeps
from primer.trigger.subscribers.agent_fresh_session import (
    AgentFreshSessionDispatcher,
)
from primer.trigger.subscribers.graph_fresh_session import (
    GraphFreshSessionDispatcher,
)


def _agent_sub(workspace_id, agent_id) -> Subscription:
    return Subscription(
        id="sb-attr-1",
        trigger_id="tr-attr-1",
        config=AgentFreshSubConfig(
            workspace_id=workspace_id, agent_id=agent_id,
        ),
        parallelism="queue",
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )


def _graph_sub(workspace_id, graph_id) -> Subscription:
    return Subscription(
        id="sb-attr-2",
        trigger_id="tr-attr-2",
        config=GraphFreshSubConfig(
            workspace_id=workspace_id, graph_id=graph_id,
        ),
        parallelism="queue",
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_agent_fresh_session_stamps_trigger_initiated_by(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_agent,
):
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    sub = _agent_sub(seeded_workspace.id, seeded_agent.id)
    res = await AgentFreshSessionDispatcher().dispatch(
        sub,
        rendered_payload="run a check",
        fire_context={"trigger_id": sub.trigger_id, "fired_at": "2026-06-01T09:00:00+00:00"},
        fire_id="fire-tr-attr-1-100",
        deps=deps,
    )
    assert res.ok and not res.skipped

    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    sess = await sessions.get(res.artefact_id)
    assert sess is not None
    assert sess.initiated_by is not None
    assert sess.initiated_by.type == "trigger"
    assert sess.initiated_by.id == sub.trigger_id
    assert sess.initiated_by.display == sub.trigger_id
    assert sess.initiated_by.role is None
    assert sess.initiated_by.source == "internal"


@pytest.mark.asyncio
async def test_graph_fresh_session_stamps_trigger_initiated_by(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_workspace_registry, seeded_workspace, seeded_graph,
):
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=fake_workspace_registry,
    )
    sub = _graph_sub(seeded_workspace.id, seeded_graph.id)
    res = await GraphFreshSessionDispatcher().dispatch(
        sub,
        rendered_payload=json.dumps({"tenant_id": "acme"}),
        fire_context={"trigger_id": sub.trigger_id},
        fire_id="fire-tr-attr-2-200",
        deps=deps,
    )
    assert res.ok

    sessions = fake_storage_provider.get_storage(WorkspaceSession)
    sess = await sessions.get(res.artefact_id)
    assert sess is not None
    assert sess.initiated_by is not None
    assert sess.initiated_by.type == "trigger"
    assert sess.initiated_by.id == sub.trigger_id
    assert sess.initiated_by.display == sub.trigger_id
    assert sess.initiated_by.role is None
    assert sess.initiated_by.source == "internal"
