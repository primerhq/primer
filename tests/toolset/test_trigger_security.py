"""Phase 3 security review findings on triggers (access-control P3 SEC 3, 6, 7): ports of the reviewer's probes, asserting the SAFE behaviour.

* A webhook trigger's token and HMAC secret are changed by an OWNER only (a full grantee may save the rest).
* A fire is refused when the trigger's run-as owner no longer holds FULL on it, or the subscription's owner no longer holds READ on it.
* Binding onto a channel trigger (generic ``create_subscription``, ``subscribe_to_channel_event``), or saving it with no config, needs READ on its channel.
"""

from __future__ import annotations

from primer.model.access import Access
from primer.model.channel import Channel
from primer.model.storage import OffsetPage
from primer.model.trigger import ChannelTriggerConfig, Subscription, Trigger
from primer.trigger.reach import refuse_fire_out_of_reach
from tests.toolset.test_trigger_tools_access import ALICE, BOB, CAROL, NOW, SID, TID, _ctx, _ref, env  # noqa: F401

_SUB = {"kind": "agent_fresh_session", "workspace_id": "ws-1", "agent_id": "ag-1"}


async def test_a_full_grantee_cannot_choose_the_webhook_token(env) -> None:
    chosen = "c" * 32

    is_err, body = await env.call("update", {"id": TID, "config": {"kind": "webhook", "token": chosen}}, CAROL)

    assert is_err and body["type"] == "forbidden", body
    assert (await env.sp.get_storage(Trigger).get(TID)).config.token == "a" * 32


async def test_a_full_grantee_cannot_set_or_clear_the_hmac_secret(env) -> None:
    is_err, body = await env.call("update", {"id": TID, "config": {"kind": "webhook", "token": "a" * 32, "hmac_secret": "s3cret-value"}}, CAROL)
    assert is_err and body["type"] == "forbidden", body
    assert (await env.sp.get_storage(Trigger).get(TID)).config.hmac_secret is None


async def test_the_owner_changes_the_token_and_a_full_grantee_still_saves_the_rest(env) -> None:
    is_err, body = await env.call("update", {"id": TID, "config": {"kind": "webhook", "token": "d" * 32}}, ALICE)
    assert not is_err, body
    assert (await env.sp.get_storage(Trigger).get(TID)).config.token == "d" * 32

    stored = (await env.sp.get_storage(Trigger).get(TID)).config.token
    is_err, body = await env.call("update", {"id": TID, "name": "renamed", "config": {"kind": "webhook", "token": stored}}, CAROL)
    assert not is_err, body
    assert (await env.sp.get_storage(Trigger).get(TID)).name == "renamed"


async def test_a_fire_is_refused_when_the_run_as_owner_lost_full_on_the_trigger(env) -> None:
    sub = await env.sp.get_storage(Subscription).get(SID)
    row = await env.sp.get_storage(Trigger).get(TID)
    await env.sp.get_storage(Trigger).update(row.model_copy(update={"owner": _ref(CAROL)}))
    assert await refuse_fire_out_of_reach(sub, env.sp) is None  # carol still holds full

    row = await env.sp.get_storage(Trigger).get(TID)
    await env.sp.get_storage(Trigger).update(row.model_copy(update={"access": Access(owners=[ALICE], grants={BOB: "read"}, rev=row.access.rev + 1)}))

    reason = await refuse_fire_out_of_reach(sub, env.sp)

    assert reason is not None and "run-as owner no longer has access" in reason and "an owner must save it" in reason


async def test_a_revoked_grantees_subscription_stops_firing(env) -> None:
    is_err, body = await env.call("create_subscription", {"trigger_id": TID, "config": _SUB}, CAROL)
    assert not is_err, body
    carols = await env.sp.get_storage(Subscription).get(body["id"])
    alices = await env.sp.get_storage(Subscription).get(SID)
    assert await refuse_fire_out_of_reach(carols, env.sp) is None

    is_err, body = await env.call("set_entity_access", {"kind": "trigger", "id": TID, "owners": [ALICE], "grants": {BOB: "read"}}, ALICE)
    assert not is_err, body

    reason = await refuse_fire_out_of_reach(carols, env.sp)
    assert reason is not None and "no longer has access to its trigger" in reason
    assert await refuse_fire_out_of_reach(alices, env.sp) is None, "the owner's own subscription still fires"


async def _private_channel_trigger(env) -> str:
    await env.sp.get_storage(Channel).create(Channel(
        id="ch-priv", provider_id="cp-1", provider="slack", external_id="CP", access=Access(owners=[ALICE]),
    ))
    await env.sp.get_storage(Trigger).create(Trigger(
        id="tr-privchan", slug="privchan", name="p", created_at=NOW, owner=_ref(ALICE),
        config=ChannelTriggerConfig(provider_id="cp-1", channel_id="ch-priv"),
        access=Access(owners=[ALICE], grants={BOB: "read", CAROL: "full"}),
    ))
    return "tr-privchan"


async def test_create_subscription_on_a_channel_trigger_needs_read_on_its_channel(env) -> None:
    tid = await _private_channel_trigger(env)

    is_err, body = await env.call("create_subscription", {"trigger_id": tid, "config": _SUB}, CAROL)

    assert is_err and body["type"] == "not-found", body
    assert (await env.sp.get_storage(Subscription).list(OffsetPage(offset=0, length=50))).total == 1, "nothing was stored"


async def test_parking_on_a_channel_trigger_needs_read_on_its_channel(env) -> None:
    from primer.model.yield_ import Yielded
    from primer.toolset.trigger import _make_subscribe_channel_handler

    tid = await _private_channel_trigger(env)

    parked = await _make_subscribe_channel_handler(env.sp)({"trigger_id": tid}, ctx=_ctx(BOB, session="s-1"))

    assert not isinstance(parked, Yielded)
    assert parked.is_error and parked.output.startswith("{") and "trigger_not_found_or_disabled" in parked.output


async def test_a_full_grantee_saving_a_channel_trigger_with_no_config_needs_read_on_its_channel(env) -> None:
    tid = await _private_channel_trigger(env)

    is_err, body = await env.call("update", {"id": tid, "name": "renamed"}, CAROL)

    assert is_err, body
    assert (await env.sp.get_storage(Trigger).get(tid)).name == "p"
