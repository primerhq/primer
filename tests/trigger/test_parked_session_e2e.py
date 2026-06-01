"""End-to-end parked_session test — Plan §11.1.

Integration acceptance test for the yielding-tool composition:

    subscribe_to_trigger  ──park──▶  WorkspaceSession.parked_status
            │                                       ▲
            └──▶ Subscription(parked_session) ──fire──┘
                            via fire_trigger + ParkedSessionDispatcher

The whole composition is exercised against the in-memory storage
provider + the fake EventBus from ``tests/trigger/conftest.py``. The
worker pool's bus listener is mocked out — instead we assert the
``respond_to_yield`` publish landed on the parked session's
``event_key`` carrying the expected tool result envelope, because
that publish IS the resume contract for the parked agent runtime.

Graph-executor composition (Plan §11.1 Step 2) is intentionally deferred:
the graph executor's resume path takes a ``tool_dispatcher`` callable
rather than reading off the event bus, so wiring an end-to-end
fire-to-resume flow requires re-implementing the worker pool's bus
listener inside the test. The agent runtime path proves the
yielding-tool composition end-to-end; the graph composition is
covered piecewise by ``tests/graph/test_toolcall_yields_writes_checkpoint.py``
and ``tests/graph/test_toolcall_resume.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from primer.int.claim import ClaimKind
from primer.model.trigger import (
    DelayedTriggerConfig,
    Subscription,
    Trigger,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import ToolContext, YieldToWorker
from primer.toolset.trigger import build_trigger_toolset_provider
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps
from primer.workspace.session_factory import SessionFactoryDeps, create_session


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_trigger(
    fake_storage_provider, *, trigger_id: str = "tr-e2e",
) -> Trigger:
    """Persist a minimal enabled delayed Trigger."""
    triggers = fake_storage_provider.get_storage(Trigger)
    t = Trigger(
        id=trigger_id,
        slug="tr-e2e",
        name="E2E Trigger",
        description=None,
        config=DelayedTriggerConfig(
            fire_at=_now() + timedelta(seconds=30),
        ),
        enabled=True,
        next_fire_at=_now() + timedelta(seconds=30),
        created_at=_now(),
    )
    await triggers.create(t)
    return t


@pytest.mark.asyncio
async def test_parked_session_e2e_subscribe_fire_resume(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_event_bus, seeded_workspace, seeded_agent,
):
    """Full yielding-tool composition: subscribe → fire → resume publish.

    Steps:
      1. Seed an enabled delayed Trigger.
      2. Create an agent-bound WorkspaceSession via the canonical factory.
      3. Invoke ``subscribe_to_trigger`` through the trigger toolset —
         asserts :class:`YieldToWorker` is raised AND a ``parked_session``
         Subscription row is persisted.
      4. Hand-park the session row (mirror what the worker pool does
         when it catches YieldToWorker — write parked_status + parked_state
         carrying the event_key + tool_call_id).
      5. Call :func:`fire_trigger` to simulate the trigger firing.
      6. Assert:
         * The Subscription row was consumed (deleted).
         * The fire result reports ok=True with no error.
         * The fake EventBus saw a publish on the parked event_key
           carrying the ``{ok, fire_context, payload}`` envelope —
           that publish is the resume contract for the parked session.
    """
    # 1. Seed the trigger.
    trigger = await _seed_trigger(fake_storage_provider)

    # 2. Create an agent-bound session via the canonical factory so
    #    the row matches what the REST router would have produced.
    session = await create_session(
        workspace_id=seeded_workspace.id,
        binding=AgentSessionBinding(agent_id=seeded_agent.id),
        initial_instructions=None,
        graph_input=None,
        auto_start=True,
        metadata=None,
        deps=SessionFactoryDeps(
            storage_provider=fake_storage_provider,
            claim_engine=fake_claim_engine,
            scheduler=fake_scheduler,
        ),
    )
    assert session.status == SessionStatus.RUNNING

    # 3. Build the trigger toolset + call subscribe_to_trigger with a
    #    ToolContext tied to the session. The handler must yield AND
    #    persist a parked_session Subscription bound to (session_id,
    #    tool_call_id).
    toolset = build_trigger_toolset_provider(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        event_bus=fake_event_bus,
    )
    tool_call_id = "tc-e2e-1"
    ctx = ToolContext(
        tool_call_id=tool_call_id,
        session_id=session.id,
        workspace_id=seeded_workspace.id,
    )
    with pytest.raises(YieldToWorker) as info:
        await toolset.call(
            tool_name="subscribe_to_trigger",
            arguments={"trigger_id": trigger.id},
            ctx=ctx,
        )

    yielded = info.value.yielded
    assert yielded.tool_name == "subscribe_to_trigger"
    assert yielded.event_key == f"trigger:{trigger.id}"
    assert info.value.tool_call_id == tool_call_id
    sub_id: str = yielded.resume_metadata["subscription_id"]
    assert sub_id.startswith("sb-")

    # The Subscription row landed before the yield surfaced — that
    # ordering protects against a fire racing the park.
    subs_storage = fake_storage_provider.get_storage(Subscription)
    sub = await subs_storage.get(sub_id)
    assert sub is not None
    assert sub.config.kind == "parked_session"
    assert sub.config.session_id == session.id
    assert sub.config.tool_call_id == tool_call_id

    # 4. Hand-park the session — mirror what the worker pool's yield
    #    handler writes when it catches YieldToWorker. The dispatcher's
    #    park-state guard reads exactly these fields.
    sessions_storage = fake_storage_provider.get_storage(WorkspaceSession)
    session.status = SessionStatus.WAITING
    session.turn_status = "idle"
    session.parked_status = "parked"
    session.parked_event_key = yielded.event_key
    session.parked_state = {
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": yielded.tool_name,
            "event_key": yielded.event_key,
            "resume_metadata": yielded.resume_metadata,
        },
    }
    await sessions_storage.update(session)

    # 5. Fire the trigger via the orchestrator. parked_session is the
    #    only enabled subscription so we expect exactly one dispatch
    #    result.
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        event_bus=fake_event_bus,
    )
    fire_res = await fire_trigger(
        trigger_id=trigger.id, scheduled_for=None, deps=deps,
    )

    # 6a. Fire orchestrator results: ok for the parked_session sub,
    #     no skip, no error code.
    assert fire_res.skipped is False
    assert fire_res.fire_id is not None
    assert len(fire_res.results) == 1
    result = fire_res.results[0]
    assert result["subscription_id"] == sub_id
    assert result["ok"] is True
    assert result.get("skipped") is False
    assert result.get("error_code") is None
    # artefact_id is the resumed session id.
    assert result.get("artefact_id") == session.id

    # 6b. Subscription row was consumed (one-shot parked_session).
    assert await subs_storage.get(sub_id) is None

    # 6c. EventBus saw the resume publish on the parked event_key with
    #     the tool result envelope. This publish IS the agent runtime's
    #     resume contract — the worker pool's bus listener flips the
    #     session to ``resumable`` from here.
    assert len(fake_event_bus.published) == 1
    key, payload = fake_event_bus.published[0]
    assert key == yielded.event_key
    assert payload["ok"] is True
    assert "fire_context" in payload
    assert payload["fire_context"]["trigger_id"] == trigger.id
    assert payload["fire_context"]["fire_id"] == fire_res.fire_id
    # No payload_template was set on the parked_session sub, so the
    # dispatcher folds fire_context through as the payload (mirrors the
    # parked_session_dispatcher fallback path for empty templates).
    assert "payload" in payload

    # 6d. Trigger row's last_fired_at was stamped and no error
    #     persisted (the fire was clean).
    triggers_storage = fake_storage_provider.get_storage(Trigger)
    updated_trigger = await triggers_storage.get(trigger.id)
    assert updated_trigger is not None
    assert updated_trigger.last_fired_at is not None
    assert updated_trigger.last_fire_error is None


@pytest.mark.asyncio
async def test_parked_session_e2e_with_payload_template(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_event_bus, seeded_workspace, seeded_agent,
):
    """Same composition + custom payload_template on the Subscription.

    The yielding tool never sets payload_template (it's structurally
    null for parked_session subs), but an operator could in principle
    update the row before fire. The dispatcher must render that
    template against the fire context and forward the result as the
    ``payload`` slot of the resume envelope.
    """
    trigger = await _seed_trigger(
        fake_storage_provider, trigger_id="tr-e2e-tpl",
    )

    session = await create_session(
        workspace_id=seeded_workspace.id,
        binding=AgentSessionBinding(agent_id=seeded_agent.id),
        initial_instructions=None,
        graph_input=None,
        auto_start=True,
        metadata=None,
        deps=SessionFactoryDeps(
            storage_provider=fake_storage_provider,
            claim_engine=fake_claim_engine,
            scheduler=fake_scheduler,
        ),
    )
    toolset = build_trigger_toolset_provider(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        event_bus=fake_event_bus,
    )
    tool_call_id = "tc-tpl-1"
    ctx = ToolContext(
        tool_call_id=tool_call_id,
        session_id=session.id,
        workspace_id=seeded_workspace.id,
    )
    with pytest.raises(YieldToWorker) as info:
        await toolset.call(
            tool_name="subscribe_to_trigger",
            arguments={"trigger_id": trigger.id},
            ctx=ctx,
        )
    sub_id = info.value.yielded.resume_metadata["subscription_id"]

    # Patch payload_template directly so the dispatcher renders against
    # the fire context. Mirrors what an operator update would do.
    subs_storage = fake_storage_provider.get_storage(Subscription)
    sub = await subs_storage.get(sub_id)
    assert sub is not None
    sub.payload_template = (
        '{"trigger_id": "{{ trigger_id }}", "marker": "custom"}'
    )
    await subs_storage.update(sub)

    # Park the session so the dispatcher will publish.
    sessions_storage = fake_storage_provider.get_storage(WorkspaceSession)
    session.status = SessionStatus.WAITING
    session.parked_status = "parked"
    session.parked_event_key = info.value.yielded.event_key
    session.parked_state = {
        "tool_call_id": tool_call_id,
        "yielded": {
            "tool_name": info.value.yielded.tool_name,
            "event_key": info.value.yielded.event_key,
            "resume_metadata": info.value.yielded.resume_metadata,
        },
    }
    await sessions_storage.update(session)

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        event_bus=fake_event_bus,
    )
    fire_res = await fire_trigger(
        trigger_id=trigger.id, scheduled_for=None, deps=deps,
    )
    assert fire_res.skipped is False
    assert len(fire_res.results) == 1
    assert fire_res.results[0]["ok"] is True

    assert len(fake_event_bus.published) == 1
    _, payload = fake_event_bus.published[0]
    # rendered_payload was JSON-parseable so dispatcher folds it into
    # the ``payload`` slot as a dict (not a raw string).
    assert payload["payload"] == {
        "trigger_id": trigger.id,
        "marker": "custom",
    }


@pytest.mark.asyncio
async def test_parked_session_e2e_chat_only_call_rejected(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_event_bus, seeded_workspace, seeded_agent,
):
    """A chat-only (no session_id) call surfaces a tool error + no row.

    Defends the upstream contract: ``subscribe_to_trigger`` MUST have
    a session to park; chat-only invocations have no resume target and
    would write an orphan row the dispatcher would just skip on fire.
    """
    trigger = await _seed_trigger(
        fake_storage_provider, trigger_id="tr-e2e-chat",
    )
    toolset = build_trigger_toolset_provider(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        event_bus=fake_event_bus,
    )
    chat_ctx = ToolContext(
        tool_call_id="tc-chat-x",
        session_id=None,  # chat-only
        workspace_id=None,
    )
    result = await toolset.call(
        tool_name="subscribe_to_trigger",
        arguments={"trigger_id": trigger.id},
        ctx=chat_ctx,
    )
    assert result.is_error
    body = json.loads(result.output)
    assert body["type"] == "trigger_not_found_or_disabled"

    # No Subscription row written — firing the trigger now is a no-op.
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        event_bus=fake_event_bus,
    )
    fire_res = await fire_trigger(
        trigger_id=trigger.id, scheduled_for=None, deps=deps,
    )
    assert fire_res.skipped is False
    assert fire_res.results == []
    assert fake_event_bus.published == []


@pytest.mark.asyncio
async def test_parked_session_e2e_session_unparked_before_fire(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_event_bus, seeded_workspace, seeded_agent,
):
    """Session abandoned the park before fire → structured skip + cleanup.

    A user-initiated session-end (or an alternative resume path) leaves
    a Subscription row pointing at a session that's no longer parked.
    The dispatcher must NOT publish on the event_key — that would wake
    code paths that already moved on — and MUST clean up the orphan row.
    """
    trigger = await _seed_trigger(
        fake_storage_provider, trigger_id="tr-e2e-unpark",
    )
    session = await create_session(
        workspace_id=seeded_workspace.id,
        binding=AgentSessionBinding(agent_id=seeded_agent.id),
        initial_instructions=None,
        graph_input=None,
        auto_start=True,
        metadata=None,
        deps=SessionFactoryDeps(
            storage_provider=fake_storage_provider,
            claim_engine=fake_claim_engine,
            scheduler=fake_scheduler,
        ),
    )
    toolset = build_trigger_toolset_provider(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        event_bus=fake_event_bus,
    )
    ctx = ToolContext(
        tool_call_id="tc-unpark-1",
        session_id=session.id,
        workspace_id=seeded_workspace.id,
    )
    with pytest.raises(YieldToWorker) as info:
        await toolset.call(
            tool_name="subscribe_to_trigger",
            arguments={"trigger_id": trigger.id},
            ctx=ctx,
        )
    sub_id = info.value.yielded.resume_metadata["subscription_id"]

    # Deliberately do NOT park the session row. The session is still
    # RUNNING — the dispatcher's park-state guard must skip + clean up.
    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        event_bus=fake_event_bus,
    )
    fire_res = await fire_trigger(
        trigger_id=trigger.id, scheduled_for=None, deps=deps,
    )
    assert fire_res.skipped is False
    assert len(fire_res.results) == 1
    result = fire_res.results[0]
    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["error_code"] == "skipped_session_unparked"

    # Orphan row was cleaned up.
    subs_storage = fake_storage_provider.get_storage(Subscription)
    assert await subs_storage.get(sub_id) is None
    # No event was published — the parked event_key has no listener.
    assert fake_event_bus.published == []


@pytest.mark.asyncio
async def test_parked_session_e2e_no_session_factory_extras(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
    fake_event_bus, seeded_workspace, seeded_agent,
):
    """Smoke: session_factory's claim/scheduler hooks fired during auto-start.

    Spot-checks that the canonical create_session path (used by REST
    + trigger fresh-session dispatchers) wired the session row into the
    scheduler + claim engine. Catches regressions where the factory
    silently drops the upserts on the fake doubles.
    """
    await create_session(
        workspace_id=seeded_workspace.id,
        binding=AgentSessionBinding(agent_id=seeded_agent.id),
        initial_instructions=None,
        graph_input=None,
        auto_start=True,
        metadata=None,
        deps=SessionFactoryDeps(
            storage_provider=fake_storage_provider,
            claim_engine=fake_claim_engine,
            scheduler=fake_scheduler,
        ),
    )
    # auto_start=True ⇒ scheduler.enqueue + claim_engine.upsert both
    # ran. Exact id is generated; just assert one call each happened.
    assert len(fake_scheduler.enqueued) == 1
    assert len(fake_claim_engine.upserts) == 1
    kind, _entity_id, _priority = fake_claim_engine.upserts[0]
    assert kind == ClaimKind.SESSION
