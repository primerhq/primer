"""fire_trigger evaluates Subscription.event_matcher against the fire event."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.model.channel import ChannelProviderType
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)
from primer.model.event_matcher import EventMatcher
from primer.model.trigger import (
    ChannelTriggerConfig,
    SessionAppendSubConfig,
    Subscription,
    Trigger,
)
from primer.trigger.dispatch import fire_trigger
from primer.trigger.subscribers import DispatchDeps


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_matching_sub_dispatches_non_matching_skipped(
    fake_storage_provider, fake_claim_engine, fake_scheduler, seeded_agent,
):
    from primer.model.workspace_session import (
        AgentSessionBinding,
        SessionStatus,
        WorkspaceSession,
    )

    triggers = fake_storage_provider.get_storage(Trigger)
    subs = fake_storage_provider.get_storage(Subscription)
    sessions = fake_storage_provider.get_storage(WorkspaceSession)

    t = Trigger(
        id="tr-c", slug="tr-c", name="channel", description=None,
        config=ChannelTriggerConfig(provider_id="channel-provider-1"),
        enabled=True,
        next_fire_at=None,
        created_at=_now(),
    )
    await triggers.create(t)

    session = WorkspaceSession(
        id="cn-1", workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id=seeded_agent.id),
        status=SessionStatus.RUNNING, turn_status="running",
        created_at=_now(),
    )
    await sessions.create(session)

    sub_open = Subscription(
        id="sb-open", trigger_id="tr-c",
        config=SessionAppendSubConfig(session_id="cn-1"),
        payload_template="hi",
        # The seeded session is mid-turn; queue so the steer is delivered
        # as a pending row rather than dropped as busy.
        parallelism="queue",
        event_matcher=EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED),
        enabled=True,
        created_at=_now(),
    )
    sub_cmd = Subscription(
        id="sb-cmd", trigger_id="tr-c",
        config=SessionAppendSubConfig(session_id="cn-1"),
        payload_template="hi",
        event_matcher=EventMatcher(
            event_type=NormalizedEventType.MESSAGE_POSTED, command_name="deploy",
        ),
        enabled=True,
        created_at=_now(),
    )
    await subs.create(sub_open)
    await subs.create(sub_cmd)

    ev = ChannelEvent(
        provider=ChannelProviderType.SLACK,
        provider_id="channel-provider-1",
        event_id="ev-1",
        type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=_now(),
        room_external_id="C123",
        surface="channel",
        message_id="m-1",
        sender=EventSender(external_id="u-1"),
        text="hello",
        command=None,
    )

    deps = DispatchDeps(
        storage_provider=fake_storage_provider,
        claim_engine=fake_claim_engine,
        scheduler=fake_scheduler,
        workspace_registry=object(),
    )
    res = await fire_trigger(
        trigger_id="tr-c", scheduled_for=None, deps=deps,
        extra_context={"event": ev.model_dump(mode="json")},
    )
    assert res.skipped is False

    by_id = {r["subscription_id"]: r for r in res.results}

    assert by_id["sb-open"]["ok"] is True
    assert by_id["sb-open"]["skipped"] is False

    assert by_id["sb-cmd"]["ok"] is True
    assert by_id["sb-cmd"]["skipped"] is True
    assert by_id["sb-cmd"]["error_code"] == "skipped_no_match"


# ---- G6-5: one match-time budget per channel event, across subscriptions and triggers ---------------------------------

_HOSTILE = r"(a|aa)+$"  # passes the save-time nested-repeat check; exponential in both engines


async def _hostile_trigger(storage, trigger_id: str, subs: int) -> None:
    await storage.get_storage(Trigger).create(Trigger(
        id=trigger_id, slug=trigger_id, name=trigger_id, description=None,
        config=ChannelTriggerConfig(provider_id="channel-provider-1"),
        enabled=True, next_fire_at=None, created_at=_now(),
    ))
    for index in range(subs):
        await storage.get_storage(Subscription).create(Subscription(
            id=f"{trigger_id}-sb-{index:02d}", trigger_id=trigger_id,
            config=SessionAppendSubConfig(session_id="cn-none"),
            payload_template="hi",
            event_matcher=EventMatcher(
                event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern=_HOSTILE,
            ),
            enabled=True, created_at=_now(),
        ))


def _hostile_event() -> dict:
    return ChannelEvent(
        provider=ChannelProviderType.SLACK, provider_id="channel-provider-1",
        event_id="ev-hostile", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=_now(), room_external_id="C123", surface="channel",
        sender=EventSender(external_id="u-1"), text="a" * 60 + "!",
    ).model_dump(mode="json")


def _deps(storage, claim_engine, scheduler) -> DispatchDeps:
    return DispatchDeps(
        storage_provider=storage, claim_engine=claim_engine,
        scheduler=scheduler, workspace_registry=object(),
    )


@pytest.mark.asyncio
async def test_one_event_spends_a_bounded_total_on_text_patterns(
    fake_storage_provider, fake_claim_engine, fake_scheduler, caplog,
):
    """Each search is bounded at 10 ms, but 30 hostile subscriptions used to
    cost 30 x 10 ms on the fire coroutine. The event's total is bounded: once
    it runs out, the remaining text_pattern subscriptions are skipped with
    their own code, and one WARNING says so."""
    import logging

    await _hostile_trigger(fake_storage_provider, "tr-hostile", 30)
    with caplog.at_level(logging.WARNING, logger="primer.trigger.dispatch"):
        res = await fire_trigger(
            trigger_id="tr-hostile", scheduled_for=None,
            deps=_deps(fake_storage_provider, fake_claim_engine, fake_scheduler),
            extra_context={"event": _hostile_event()},
        )

    codes = [r["error_code"] for r in res.results]
    assert len(codes) == 30
    assert all(r["ok"] is True and r["skipped"] is True for r in res.results)
    budget_skips = codes.count("skipped_match_budget")
    # 100 ms of budget at 10 ms per search: at most ~10 subscriptions are matched.
    assert budget_skips >= 18, codes
    assert set(codes) <= {"skipped_no_match", "skipped_match_budget"}
    warnings = [r for r in caplog.records if "budget" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert warnings[0].levelno == logging.WARNING
    assert "ev-hostile" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_the_budget_is_shared_by_every_trigger_one_event_fires(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
):
    from primer.model.event_matcher import channel_event_text_budget

    await _hostile_trigger(fake_storage_provider, "tr-first", 12)
    await _hostile_trigger(fake_storage_provider, "tr-second", 5)
    deps = _deps(fake_storage_provider, fake_claim_engine, fake_scheduler)
    with channel_event_text_budget():
        await fire_trigger(
            trigger_id="tr-first", scheduled_for=None, deps=deps,
            extra_context={"event": _hostile_event()},
        )
        second = await fire_trigger(
            trigger_id="tr-second", scheduled_for=None, deps=deps,
            extra_context={"event": _hostile_event()},
        )
    assert [r["error_code"] for r in second.results] == ["skipped_match_budget"] * 5


@pytest.mark.asyncio
async def test_a_subscription_without_a_text_pattern_still_matches_after_the_budget_ran_out(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
):
    from primer.model.event_matcher import channel_event_text_budget

    await _hostile_trigger(fake_storage_provider, "tr-mixed", 12)
    await fake_storage_provider.get_storage(Subscription).create(Subscription(
        id="tr-mixed-sb-zz-plain", trigger_id="tr-mixed",
        config=SessionAppendSubConfig(session_id="cn-none"),
        payload_template="hi",
        event_matcher=EventMatcher(
            event_type=NormalizedEventType.MESSAGE_POSTED, command_name="deploy",
        ),
        enabled=True, created_at=_now(),
    ))
    with channel_event_text_budget() as budget:
        res = await fire_trigger(
            trigger_id="tr-mixed", scheduled_for=None,
            deps=_deps(fake_storage_provider, fake_claim_engine, fake_scheduler),
            extra_context={"event": _hostile_event()},
        )
    assert budget.exhausted
    by_id = {r["subscription_id"]: r for r in res.results}
    # Its other fields decide, not the spent budget: no command, so no match.
    assert by_id["tr-mixed-sb-zz-plain"]["error_code"] == "skipped_no_match"
