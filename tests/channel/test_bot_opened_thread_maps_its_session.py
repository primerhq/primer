"""W35: a thread the bot opens for a session is mapped to it, so a reply there steers that session.

Only ask_user used to write the thread-to-session row; a reply in the thread of an inform or approval post
started a second session in the same thread. A shared anchor (a thread the session did not open) gets no row.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import primer.channel.event_dispatch as ed
from primer.channel.correlation import CorrelationStore
from primer.channel.event_dispatch import ChannelEventRouter
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import ChannelEvent, EventSender, NormalizedEventType
from primer.session.steer_delivery import SteerDelivery
from primer.trigger.subscribers import DispatchDeps
from tests.channel.test_outbound_posts_characterization import _env, discord, slack
from tests.conftest import _FakeStorageProvider


@pytest.mark.parametrize(("make", "anchor"), [(slack, "t-1"), (discord, "501")], ids=["slack", "discord"])
@pytest.mark.parametrize("kind", ["inform", "tool_approval"])
async def test_bot_opened_thread_is_mapped(make, anchor: str, kind: str) -> None:
    sp = _FakeStorageProvider()
    a, _ = make(sp)
    await a.post_prompt(_env(kind, tool_name="t", tool_args={}))
    row = await CorrelationStore(sp).lookup("ch-1", anchor)
    assert row is not None
    assert (row.kind, row.workspace_id, row.session_id, row.tool_call_id) == ("session", "ws-1", "s1", None)


async def test_a_reused_thread_is_not_rewritten() -> None:
    sp = _FakeStorageProvider()
    a, client = slack(sp)
    await a.post_prompt(_env("inform"))
    await a.post_prompt(_env("inform", prompt="again"))
    assert len(await CorrelationStore(sp).list_for_channel("ch-1")) == 1
    assert [p.get("thread_ts") for p in client.posts] == [None, "t-1", "t-1"]


async def test_reply_in_the_inform_thread_steers_the_session(monkeypatch) -> None:
    sp = _FakeStorageProvider()
    a, _ = slack(sp)
    await a.post_prompt(_env("inform"))
    captured: dict = {}

    async def _fake_deliver(**kw):
        captured.update(kw)
        return SteerDelivery(outcome="woken", session_id=kw["session_id"])

    monkeypatch.setattr(ed, "deliver_steer", _fake_deliver)
    router = ChannelEventRouter(
        storage_provider=sp, correlation_store=CorrelationStore(sp),
        fire_deps=DispatchDeps(storage_provider=sp, claim_engine=object(), scheduler=object(), workspace_registry=object()),
        event_bus=None,
    )
    event = ChannelEvent(
        provider=ChannelProviderType.SLACK, provider_id="cp", event_id="ev-1", type=NormalizedEventType.MESSAGE_POSTED,
        occurred_at=datetime.now(UTC), channel_id="ch-1", surface="thread", thread_anchor="t-1",
        sender=EventSender(external_id="U1"), text="and the tests?",
    )
    outcome = await router.route_event(event=event, channel=None)
    assert (outcome.kind, outcome.session_id) == ("steer", "s1")
    assert captured["text"] == "and the tests?"


async def test_a_failed_row_write_does_not_fail_the_post(monkeypatch) -> None:
    sp = _FakeStorageProvider()
    a, client = slack(sp)

    async def _boom(self, **kw):
        raise RuntimeError("storage down")

    monkeypatch.setattr(CorrelationStore, "upsert_thread_session", _boom)
    await a.post_prompt(_env("inform"))
    assert client.posts[-1]["text"].endswith("Done: all good")
