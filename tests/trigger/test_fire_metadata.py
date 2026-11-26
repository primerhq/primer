"""Trusted fire classification is isolated from payloads and sibling dispatches."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from primer.model.except_ import NotFoundError
from primer.model.trigger import (
    ChannelTriggerConfig,
    DelayedTriggerConfig,
    ScheduledTriggerConfig,
    SessionAppendSubConfig,
    Subscription,
    Trigger,
    WebhookTriggerConfig,
)
import primer.trigger.dispatch as dispatch_module
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps, SubscriptionDispatchResult, TriggerFireMetadata


NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)
CONFIGS = [
    pytest.param(DelayedTriggerConfig(fire_at=NOW), False, id="delayed"),
    pytest.param(ScheduledTriggerConfig(cron="0 * * * *"), False, id="scheduled"),
    pytest.param(WebhookTriggerConfig(), False, id="webhook-default"),
    pytest.param(WebhookTriggerConfig(interactive=True), True, id="webhook-interactive"),
    pytest.param(ChannelTriggerConfig(provider_id="p"), True, id="channel-default"),
    pytest.param(ChannelTriggerConfig(provider_id="p", interactive=False), False, id="channel-quiet"),
]


async def _seed(sp, config, *, id="tr-fire", count=2):
    trigger = Trigger(id=id, slug=id, name=id, config=config, created_at=NOW)
    await sp.get_storage(Trigger).create(trigger)
    for i in range(count):
        await sp.get_storage(Subscription).create(Subscription(
            id=f"{id}-sub-{i}", trigger_id=id,
            config=SessionAppendSubConfig(session_id="unused"),
            payload_template="{{ kind }}", created_at=NOW,
        ))
    return trigger


@pytest.mark.parametrize("config,interactive", CONFIGS)
async def test_classification_uses_trigger_snapshot_not_payload(
    fake_storage_provider, monkeypatch, config, interactive,
):
    sp = fake_storage_provider
    await _seed(sp, config)
    collaborators = [object() for _ in range(4)]
    deps = DispatchDeps(sp, *collaborators)
    seen = []

    class LegacySignatureDispatcher:
        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            seen.append((deps, deps.fire_metadata, rendered_payload, dict(fire_context)))
            return SubscriptionDispatchResult(ok=True)

    monkeypatch.setattr(dispatch_module, "get_dispatcher", lambda kind: LegacySignatureDispatcher())
    spoof = {"kind": "forged", "trigger_id": "forged", "interactive": not interactive,
             "fire_metadata": {"interactive": not interactive}}
    result = await fire_trigger(trigger_id="tr-fire", scheduled_for=None, deps=deps, extra_context=spoof)

    assert len(seen) == 2
    assert all(r["ok"] for r in result.results)
    assert deps.fire_metadata is None
    assert seen[0][0] is not seen[1][0] and all(item[0] is not deps for item in seen)
    for delivered_deps, metadata, rendered, context in seen:
        assert metadata.trigger_id == "tr-fire"
        assert metadata.kind == config.kind
        assert metadata.interactive is interactive
        assert rendered == "forged"  # Existing payload overlay semantics are unchanged.
        assert context["fire_metadata"] == spoof["fire_metadata"]
        assert context["interactive"] is not interactive
        assert delivered_deps.storage_provider is sp
        assert [delivered_deps.claim_engine, delivered_deps.scheduler,
                delivered_deps.workspace_registry, delivered_deps.event_bus] == collaborators
        with pytest.raises(FrozenInstanceError):
            metadata.interactive = not interactive
    assert seen[0][1] is seen[1][1]


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("change", ["edit", "delete"])
async def test_edit_or_delete_during_fanout_keeps_original_classification(
    fake_storage_provider, monkeypatch, interactive, change,
):
    sp = fake_storage_provider
    original = await _seed(sp, WebhookTriggerConfig(interactive=interactive))
    deps = DispatchDeps(sp, None)
    seen = []

    class MutatingDispatcher:
        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            seen.append(deps.fire_metadata)
            if len(seen) == 1:
                rows = sp.get_storage(Trigger)
                if change == "delete":
                    await rows.delete(original.id)
                else:
                    await rows.update(original.model_copy(update={
                        "config": WebhookTriggerConfig(interactive=not interactive),
                    }))
                deps.fire_metadata = None
                fire_context.update(kind="forged", interactive=not interactive)
            return SubscriptionDispatchResult(ok=True)

    monkeypatch.setattr(dispatch_module, "get_dispatcher", lambda kind: MutatingDispatcher())
    if change == "delete":
        # Existing final bookkeeping still reports a deleted trigger; both subscribers ran first.
        with pytest.raises(NotFoundError):
            await fire_trigger(trigger_id=original.id, scheduled_for=None, deps=deps)
    else:
        result = await fire_trigger(trigger_id=original.id, scheduled_for=None, deps=deps)
        assert all(r["ok"] for r in result.results)
        assert (await sp.get_storage(Trigger).get(original.id)).config.interactive is not interactive
    assert len(seen) == 2
    assert all(m is not None and m.interactive is interactive for m in seen)
    assert seen[0] is seen[1]
    assert deps.fire_metadata is None


async def test_concurrent_fires_do_not_mutate_shared_dependencies(fake_storage_provider, monkeypatch):
    sp = fake_storage_provider
    await _seed(sp, WebhookTriggerConfig(interactive=True), id="tr-one", count=1)
    await _seed(sp, WebhookTriggerConfig(), id="tr-two", count=1)
    deps = DispatchDeps(sp, None)
    entered = {id: asyncio.Event() for id in ("tr-one", "tr-two")}
    seen = []

    class ConcurrentDispatcher:
        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            metadata = deps.fire_metadata
            entered[sub.trigger_id].set()
            other = "tr-two" if sub.trigger_id == "tr-one" else "tr-one"
            await asyncio.wait_for(entered[other].wait(), timeout=2)
            seen.append((sub.trigger_id, metadata, deps.fire_metadata))
            return SubscriptionDispatchResult(ok=True)

    monkeypatch.setattr(dispatch_module, "get_dispatcher", lambda kind: ConcurrentDispatcher())
    results = await asyncio.gather(*(
        fire_trigger(trigger_id=id, scheduled_for=None, deps=deps) for id in entered
    ))
    assert len(seen) == 2
    assert all(r.results[0]["ok"] for r in results)
    for id, before, after in seen:
        assert before is after
        assert after.trigger_id == id
        assert after.interactive is (id == "tr-one")
    assert deps.fire_metadata is None


async def test_dispatch_failure_does_not_change_sibling_metadata(fake_storage_provider, monkeypatch):
    await _seed(fake_storage_provider, WebhookTriggerConfig(interactive=True))
    deps = DispatchDeps(fake_storage_provider, None)
    seen = []

    class FailingDispatcher:
        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            seen.append(deps.fire_metadata)
            if len(seen) == 1:
                deps.fire_metadata = None
                raise RuntimeError("failed first subscriber")
            return SubscriptionDispatchResult(ok=True)

    monkeypatch.setattr(dispatch_module, "get_dispatcher", lambda kind: FailingDispatcher())
    result = await fire_trigger(trigger_id="tr-fire", scheduled_for=None, deps=deps)
    assert [r["ok"] for r in result.results] == [False, True]
    assert len(seen) == 2 and seen[0] is seen[1] and seen[1].interactive is True
    assert deps.fire_metadata is None


async def test_cancelled_fire_leaves_reusable_dependencies_unmodified(fake_storage_provider, monkeypatch):
    await _seed(fake_storage_provider, WebhookTriggerConfig(interactive=True), count=1)
    deps = DispatchDeps(fake_storage_provider, None)
    seen = []

    class CancelledDispatcher:
        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            seen.append(deps.fire_metadata)
            raise asyncio.CancelledError

    monkeypatch.setattr(dispatch_module, "get_dispatcher", lambda kind: CancelledDispatcher())
    with pytest.raises(asyncio.CancelledError):
        await fire_trigger(trigger_id="tr-fire", scheduled_for=None, deps=deps)
    assert len(seen) == 1 and seen[0].interactive is True
    assert deps.fire_metadata is None


def test_legacy_dependencies_have_unknown_classification():
    deps = DispatchDeps(object(), None)
    assert deps.fire_metadata is None  # Absence is not a non-interactive classification.


async def test_nested_fire_replaces_inherited_metadata_without_changing_outer_fire(
    fake_storage_provider, monkeypatch,
):
    sp = fake_storage_provider
    await _seed(sp, WebhookTriggerConfig(interactive=True), id="tr-outer", count=1)
    await _seed(sp, WebhookTriggerConfig(), id="tr-inner", count=1)
    deps = DispatchDeps(sp, None)
    seen = []

    class NestedDispatcher:
        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            before = deps.fire_metadata
            if sub.trigger_id == "tr-outer":
                inner = await fire_trigger(trigger_id="tr-inner", scheduled_for=None, deps=deps)
                seen.append(("inner_ok", inner.results[0]["ok"]))
            seen.append((sub.trigger_id, before, deps.fire_metadata))
            return SubscriptionDispatchResult(ok=True)

    monkeypatch.setattr(dispatch_module, "get_dispatcher", lambda kind: NestedDispatcher())
    result = await fire_trigger(trigger_id="tr-outer", scheduled_for=None, deps=deps)
    assert result.results[0]["ok"]
    assert seen[1] == ("inner_ok", True)
    assert [(item[0], item[1].trigger_id, item[1].interactive, item[1] is item[2])
            for item in (seen[0], seen[2])] == [
                ("tr-inner", "tr-inner", False, True), ("tr-outer", "tr-outer", True, True),
            ]
    assert deps.fire_metadata is None


async def test_metadata_is_captured_before_source_builds_payload(fake_storage_provider, monkeypatch):
    from primer.model.trigger import TriggerKind
    from primer.trigger.sources.webhook import WebhookSource

    await _seed(fake_storage_provider, WebhookTriggerConfig(interactive=True), count=1)
    stale = TriggerFireMetadata(trigger_id="old", kind=TriggerKind.DELAYED, interactive=False)
    deps = DispatchDeps(fake_storage_provider, None, fire_metadata=stale)
    seen = []

    class MutatingSource(WebhookSource):
        def build_fire_context(self, trigger, **kwargs):
            trigger.config.interactive = False
            return super().build_fire_context(trigger, **kwargs)

    class RecordingDispatcher:
        async def dispatch(self, sub, *, rendered_payload, fire_context, fire_id, deps):
            seen.append((deps.fire_metadata, dict(fire_context)))
            return SubscriptionDispatchResult(ok=True)

    monkeypatch.setattr(dispatch_module, "get_source", lambda kind: MutatingSource())
    monkeypatch.setattr(dispatch_module, "get_dispatcher", lambda kind: RecordingDispatcher())
    result = await fire_trigger(trigger_id="tr-fire", scheduled_for=None, deps=deps)
    assert result.results[0]["ok"] and len(seen) == 1
    assert seen[0][0].trigger_id == "tr-fire" and seen[0][0].interactive is True
    assert "fire_metadata" not in seen[0][1] and "interactive" not in seen[0][1]
    assert deps.fire_metadata is stale


@pytest.mark.parametrize("config,interactive", CONFIGS)
async def test_fresh_agent_and_graph_store_priority_from_the_same_fire(
    fake_storage_provider, fake_claim_engine, fake_scheduler, fake_workspace_registry,
    seeded_workspace, seeded_agent, seeded_graph, monkeypatch, config, interactive,
):
    from primer.model.principal import PrincipalRef
    from primer.model.trigger import AgentFreshSubConfig, GraphFreshSubConfig
    from primer.model.workspace_session import WorkspaceSession

    sp = fake_storage_provider
    triggers = sp.get_storage(Trigger)
    trigger = await triggers.create(Trigger(id="tr-priority", slug="priority", name="priority", config=config, created_at=NOW))
    cases = [
        (seeded_agent, AgentFreshSubConfig(workspace_id=seeded_workspace.id, agent_id=seeded_agent.id), 19),
        (seeded_graph, GraphFreshSubConfig(workspace_id=seeded_workspace.id, graph_id=seeded_graph.id), 73),
    ]
    for source, sub_config, value in cases:
        await sp.get_storage(type(source)).update(source.model_copy(update={"default_priority": value}))
        await sp.get_storage(Subscription).create(Subscription(
            id=f"sub-{source.id}", trigger_id=trigger.id, config=sub_config, payload_template="{}",
            owner=PrincipalRef.system(), parallelism="queue", created_at=NOW,
        ))
    deps = DispatchDeps(sp, fake_claim_engine, fake_scheduler, fake_workspace_registry)
    extra = {"interactive": not interactive, "priority": 1, "fire_metadata": {"interactive": not interactive}}

    # Actual dispatch with ordinary production dependencies accepts the binding defaults.
    # Change the live webhook row after the first real child was published, before its sibling starts.
    if isinstance(config, WebhookTriggerConfig):
        original_upsert = fake_claim_engine.upsert
        calls = 0

        async def upsert(*args, **kwargs):
            nonlocal calls
            result = await original_upsert(*args, **kwargs)
            calls += 1
            if calls == 1:
                if interactive:
                    await triggers.delete(trigger.id)
                else:
                    await triggers.update(trigger.model_copy(update={"config": WebhookTriggerConfig(interactive=True)}))
            return result

        monkeypatch.setattr(fake_claim_engine, "upsert", upsert)

    if not interactive:
        result = await fire_trigger(trigger_id=trigger.id, scheduled_for=NOW, deps=deps, extra_context=extra)
        assert len(result.results) == 2 and all(r["ok"] and not r["skipped"] for r in result.results)
    else:
        if isinstance(config, WebhookTriggerConfig):
            # Both subscribers complete before final trigger bookkeeping observes the deletion.
            with pytest.raises(NotFoundError):
                await fire_trigger(trigger_id=trigger.id, scheduled_for=None, deps=deps, extra_context=extra)
        else:
            result = await fire_trigger(trigger_id=trigger.id, scheduled_for=None, deps=deps, extra_context=extra)
            assert len(result.results) == 2 and all(r["ok"] and not r["skipped"] for r in result.results)
    children = list(sp.get_storage(WorkspaceSession)._data.values())
    assert len(children) == 2
    assert {row.metadata["subscription_id"]: row.priority for row in children} == {
        f"sub-{source.id}": 0 if interactive else value for source, _, value in cases
    }
