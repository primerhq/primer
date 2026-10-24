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


# ---- round 2, M1: one owner's slow subscriptions cannot starve another owner's -----------------------------------------


@pytest.mark.asyncio
async def test_each_owner_matches_under_its_own_share_of_the_event_budget(
    fake_storage_provider, fake_claim_engine, fake_scheduler,
):
    from primer.model.principal import PrincipalRef

    def _owner(uid: str) -> PrincipalRef:
        return PrincipalRef(type="user", id=uid, display=uid, role="user", source="local")

    await fake_storage_provider.get_storage(Trigger).create(Trigger(
        id="tr-owners", slug="tr-owners", name="tr-owners", description=None,
        config=ChannelTriggerConfig(provider_id="channel-provider-1"),
        enabled=True, next_fire_at=None, created_at=_now(),
    ))
    subs = fake_storage_provider.get_storage(Subscription)
    for index in range(10):  # owner A: ten slow subscriptions, matched first
        await subs.create(Subscription(
            id=f"tr-owners-a-{index:02d}", trigger_id="tr-owners",
            config=SessionAppendSubConfig(session_id="cn-none"), payload_template="hi",
            event_matcher=EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern=_HOSTILE),
            enabled=True, created_at=_now(), owner=_owner("u-a"),
        ))
    await subs.create(Subscription(
        id="tr-owners-b", trigger_id="tr-owners",
        config=SessionAppendSubConfig(session_id="cn-none"), payload_template="hi",
        event_matcher=EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern=r"^a{3}"),
        enabled=True, created_at=_now(), owner=_owner("u-b"),
    ))

    res = await fire_trigger(
        trigger_id="tr-owners", scheduled_for=None,
        deps=_deps(fake_storage_provider, fake_claim_engine, fake_scheduler),
        extra_context={"event": _hostile_event()},
    )

    by_id = {r["subscription_id"]: r for r in res.results}
    a_codes = [by_id[f"tr-owners-a-{index:02d}"]["error_code"] for index in range(10)]
    # A's share is 50 ms of the 100: about five 10 ms searches, then A's later subscriptions are skipped.
    assert a_codes.count("skipped_match_budget") >= 3, a_codes
    # B's simple subscription is still matched: whatever its dispatcher answers, it was not skipped by the matcher.
    assert by_id["tr-owners-b"]["error_code"] not in {"skipped_match_budget", "skipped_no_match"}, by_id["tr-owners-b"]


@pytest.mark.asyncio
async def test_each_owner_key_is_the_user_behind_the_subscription(fake_storage_provider):
    """R2-1 (a): an API token counts as its user; R2-6: a subscription with no owner is its own owner."""
    from primer.model.api_token import ApiToken
    from primer.model.principal import PrincipalRef
    from primer.trigger.dispatch import enabled_subscriptions, owner_keys

    await fake_storage_provider.get_storage(ApiToken).create(ApiToken(
        id="tok-1", user_id="u-a", name="t1", token_hash="0" * 64, prefix="prmr_abc", created_at=_now(),
    ))
    await fake_storage_provider.get_storage(Trigger).create(Trigger(
        id="tr-count", slug="tr-count", name="tr-count", description=None,
        config=ChannelTriggerConfig(provider_id="channel-provider-1"),
        enabled=True, next_fire_at=None, created_at=_now(),
    ))
    subs = fake_storage_provider.get_storage(Subscription)
    owners = {
        "by-user": PrincipalRef(type="user", id="u-a", display="u-a", role="user", source="local"),
        "by-token": PrincipalRef(type="api_token", id="tok-1", display="t1", role="user", source="internal"),
        "by-gone-token": PrincipalRef(type="api_token", id="tok-gone", display="t9", role="user", source="internal"),
        "by-b": PrincipalRef(type="user", id="u-b", display="u-b", role="user", source="local"),
        "no-owner-1": None,
        "no-owner-2": None,
        "unattributed": PrincipalRef.unattributed(),
    }
    for sub_id, owner in owners.items():
        await subs.create(Subscription(
            id=sub_id, trigger_id="tr-count",
            config=SessionAppendSubConfig(session_id="cn-none"), payload_template="hi",
            event_matcher=EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern="x"),
            enabled=True, created_at=_now(), owner=owner,
        ))
    await subs.create(Subscription(
        id="no-text", trigger_id="tr-count",
        config=SessionAppendSubConfig(session_id="cn-none"), payload_template="hi",
        event_matcher=EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED),
        enabled=True, created_at=_now(),
    ))
    keys = await owner_keys(fake_storage_provider, await enabled_subscriptions(fake_storage_provider, "tr-count"))
    assert keys == {
        "by-user": ("user", "u-a"),
        "by-token": ("user", "u-a"),
        "by-gone-token": ("api_token", "tok-gone"),
        "by-b": ("user", "u-b"),
        "no-owner-1": ("subscription", "no-owner-1"),
        "no-owner-2": ("subscription", "no-owner-2"),
        "unattributed": ("subscription", "unattributed"),
    }


@pytest.mark.asyncio
async def test_many_tokens_of_one_user_cannot_starve_another_user(
    fake_storage_provider, fake_claim_engine, fake_scheduler, monkeypatch,
):
    """R2-1: the review's probe as a test. 25 API tokens of one user, each with a near-cap pattern whose cold
    compile is slow, used to be 25 owners whose overshooting compiles spent the whole pool; the second user's
    simple subscription was then skipped. Now the tokens are one user with one share, and a share charges the
    pool at most what it had left."""
    import time as _time

    import primer.model.event_matcher as event_matcher_module
    from primer.model.api_token import ApiToken
    from primer.model.principal import PrincipalRef

    real = event_matcher_module._compile_text_pattern

    def _cold_compile(pattern):
        _time.sleep(0.01)  # a cold near-cap compile after a restart, made deterministic
        return real(pattern)

    monkeypatch.setattr(event_matcher_module, "_compile_text_pattern", _cold_compile)
    await fake_storage_provider.get_storage(Trigger).create(Trigger(
        id="tr-tokens", slug="tr-tokens", name="tr-tokens", description=None,
        config=ChannelTriggerConfig(provider_id="channel-provider-1"),
        enabled=True, next_fire_at=None, created_at=_now(),
    ))
    tokens = fake_storage_provider.get_storage(ApiToken)
    subs = fake_storage_provider.get_storage(Subscription)
    for k in range(25):
        await tokens.create(ApiToken(
            id=f"tok-{k:02d}", user_id="u-attacker", name=f"t{k}", token_hash=f"{k:064d}",
            prefix="prmr_xyz", created_at=_now(),
        ))
        await subs.create(Subscription(
            id=f"tr-tokens-a-{k:02d}", trigger_id="tr-tokens",
            config=SessionAppendSubConfig(session_id="cn-none"), payload_template="hi",
            event_matcher=EventMatcher(
                event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern=f"(?:a{{100}}){{99}}q{k:03d}",
            ),
            enabled=True, created_at=_now(),
            owner=PrincipalRef(type="api_token", id=f"tok-{k:02d}", display=f"t{k}", role="user", source="internal"),
        ))
    await subs.create(Subscription(
        id="tr-tokens-victim", trigger_id="tr-tokens",
        config=SessionAppendSubConfig(session_id="cn-none"), payload_template="hi",
        event_matcher=EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern="deploy"),
        enabled=True, created_at=_now(),
        owner=PrincipalRef(type="user", id="u-victim", display="victim", role="user", source="local"),
    ))
    event = ChannelEvent(
        provider=ChannelProviderType.SLACK, provider_id="channel-provider-1",
        event_id="ev-tokens", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=_now(), room_external_id="C123", surface="channel",
        sender=EventSender(external_id="u-1"), text="the deploy of build 4512 finished",
    ).model_dump(mode="json")

    res = await fire_trigger(
        trigger_id="tr-tokens", scheduled_for=None,
        deps=_deps(fake_storage_provider, fake_claim_engine, fake_scheduler),
        extra_context={"event": event},
    )

    by_id = {r["subscription_id"]: r for r in res.results}
    attacker = [by_id[f"tr-tokens-a-{k:02d}"]["error_code"] for k in range(25)]
    assert attacker.count("skipped_match_budget") >= 15, attacker  # one 50 ms share, 10 ms a compile
    assert by_id["tr-tokens-victim"]["error_code"] not in {"skipped_match_budget", "skipped_no_match"}, by_id["tr-tokens-victim"]


@pytest.mark.asyncio
async def test_the_router_reads_each_triggers_subscriptions_once(tmp_path, async_closers, monkeypatch):
    """R2-11: the subscriptions the router read for the owner shares reach fire_trigger; they are not read twice."""
    import primer.channel.event_dispatch as event_dispatch_module
    import primer.trigger.dispatch as dispatch_module
    from primer.channel.correlation import CorrelationStore
    from primer.channel.event_dispatch import ChannelEventRouter
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "r.sqlite"))
    await provider.initialize()
    async_closers.push_async_callback(provider.aclose)
    for trigger_id in ("tr-r1", "tr-r2"):
        await provider.get_storage(Trigger).create(Trigger(
            id=trigger_id, slug=trigger_id, name=trigger_id, description=None,
            config=ChannelTriggerConfig(provider_id="channel-provider-1"),
            enabled=True, next_fire_at=None, created_at=_now(),
        ))
        await provider.get_storage(Subscription).create(Subscription(
            id=f"{trigger_id}-sub", trigger_id=trigger_id,
            config=SessionAppendSubConfig(session_id="cn-none"), payload_template="hi",
            event_matcher=EventMatcher(event_type=NormalizedEventType.MESSAGE_POSTED, text_pattern="nothing-here"),
            enabled=True, created_at=_now(),
        ))
    reads: list[str] = []
    real = dispatch_module.enabled_subscriptions

    async def _counting(storage_provider, trigger_id):
        reads.append(trigger_id)
        return await real(storage_provider, trigger_id)

    monkeypatch.setattr(dispatch_module, "enabled_subscriptions", _counting)
    monkeypatch.setattr(event_dispatch_module, "enabled_subscriptions", _counting)
    router = ChannelEventRouter(
        storage_provider=provider, correlation_store=CorrelationStore(provider),
        fire_deps=DispatchDeps(storage_provider=provider, claim_engine=None, scheduler=None),
    )
    event = ChannelEvent(
        provider=ChannelProviderType.SLACK, provider_id="channel-provider-1",
        event_id="ev-once", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=_now(), room_external_id="C123", surface="channel",
        sender=EventSender(external_id="u-1"), text="hello",
    )
    await router.route_event(event=event, channel=None)
    assert sorted(reads) == ["tr-r1", "tr-r2"], reads
