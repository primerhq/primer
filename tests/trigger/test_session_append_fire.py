"""S6 P1: a delayed trigger with a session_append sub steers its target.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 3. This is the
fire-level contract Task 10's interactive hold reads: results carry the
target session as the artefact_id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import primer.session.steer_delivery as sd
from primer.model.principal import PrincipalRef
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
from primer.model.trigger import DelayedTriggerConfig, SessionAppendSubConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.trigger.dispatch import fire_trigger
from primer.trigger.service import (
    ServiceDeps,
    create_subscription,
    create_trigger,
)
from primer.trigger.subscribers import DispatchDeps
from tests.conftest import _FakeStorageProvider


async def test_fire_steers_the_target_session(monkeypatch):
    sp = _FakeStorageProvider()
    # The target session's workspace: the subscription's owner must hold full on it when it is saved (access-control P2 T6).
    await sp.get_storage(Workspace).create(Workspace(
        id="w1", template_id="t", provider_id="p", created_at=datetime.now(UTC),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://w1", token="t"),
    ))
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s-target",
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.WAITING,
        created_at=datetime.now(UTC),
        turn_status="idle",
    ))
    woken: list[str] = []

    async def _fake_wake(**kw):
        woken.append(kw["instruction"])

    monkeypatch.setattr(sd, "wake_session", _fake_wake)

    deps = ServiceDeps(storage_provider=sp)
    trigger = await create_trigger(
        slug="sa-fire",
        name="Append",
        description=None,
        config=DelayedTriggerConfig(
            fire_at=datetime.now(UTC) + timedelta(hours=1)
        ),
        enabled=True,
        deps=deps,
    )
    await create_subscription(
        trigger_id=trigger.id,
        config=SessionAppendSubConfig(session_id="s-target"),
        payload_template="status check",
        parallelism="queue",
        owner=PrincipalRef.system(),  # a fire of an ownerless subscription is refused (access-control P3 Task 4)
        deps=deps,
    )

    result = await fire_trigger(
        trigger_id=trigger.id,
        scheduled_for=None,
        deps=DispatchDeps(
            storage_provider=sp,
            claim_engine=object(),
            scheduler=object(),
            workspace_registry=object(),
        ),
    )

    assert result.skipped is False
    assert len(result.results) == 1
    assert result.results[0]["ok"] is True
    assert result.results[0]["artefact_id"] == "s-target"
    assert woken == ["status check"]
