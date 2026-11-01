"""Channel provider inbound handlers (Part 2 Task 75: W36, W56a, W56c, W56d, W56e, W56f).

* W36: a Telegram quote-reply that answers no open question and no reject prompt was dropped.
* W56a: Telegram entity offsets count UTF-16 code units, and a caption's entities are ``caption_entities``.
* W56c: a Discord reject modal was decided twice (its ``on_submit`` and the ``on_interaction`` fallback).
* W56d: a Slack reject in a thread lost the tool and args blocks (``conversations.history`` has no thread replies).
* W56e: a Slack ``thread_broadcast`` reply was dropped by the subtype filter.
* W56f (D13): a Slack DM is ``surface="dm"``, so the DM continues one session, as on Discord and Telegram.

The handlers are driven through the platform test fakes; no live Slack, Discord or Telegram.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from primer.channel.discord import factory as discord_factory
from primer.channel.event_dispatch import mapping_anchor
from primer.channel.slack import factory as slack_factory
from primer.channel.slack.normalizer import SlackEventNormalizer
from primer.channel.telegram import factory as tg_factory
from primer.channel.telegram.normalizer import _mentions_bot
from tests.channel.discord import test_factory as dc
from tests.channel.slack import test_factory as sl
from tests.channel.telegram import test_factory as tg


@pytest.fixture(autouse=True)
def _fresh_handler_registries(monkeypatch):
    """The imported helpers number provider ids from their modules' counters, which another module's tests may already have
    installed under; fresh handler registries keep every install here real."""
    for module in (discord_factory, slack_factory, tg_factory):
        monkeypatch.setattr(module, "_HANDLERS_INSTALLED", set())


def _corr(monkeypatch, rec):
    class Corr:
        def __init__(self, sp):
            pass

        async def lookup(self, cid, key):
            return rec

        async def clear(self, cid, key):
            pass

        async def clear_gate(self, cid, key, tool_call_id=None):
            pass

    monkeypatch.setattr("primer.channel.correlation.CorrelationStore", Corr)


# ---- characterization ------------------------------------------------------------------------------------------------


async def test_a_correlated_telegram_reply_answers_the_question_and_is_not_routed(monkeypatch):
    adapter = tg._mock_adapter()
    _, on_message = tg._install(monkeypatch, tg._FakeEntry({"100": adapter}))
    route = AsyncMock(return_value=True)
    monkeypatch.setattr(tg_factory, "_route_channel_event", route)
    _corr(monkeypatch, SimpleNamespace(kind="session", workspace_id="w", session_id="s", tool_call_id="t"))

    await on_message(SimpleNamespace(message=tg._msg(text="blue", reply_to=SimpleNamespace(message_id=33))), tg._context())

    adapter._handle_text_reply.assert_awaited_once()
    route.assert_not_awaited()


async def test_a_telegram_reply_to_a_reject_prompt_decides_and_is_not_routed(monkeypatch):
    adapter = tg._mock_adapter()
    adapter.resolve_reply_target = MagicMock(return_value={
        "kind": "reject", "workspace_id": "w", "session_id": "s", "tool_call_id": "t"})
    _, on_message = tg._install(monkeypatch, tg._FakeEntry({"100": adapter}))
    route = AsyncMock(return_value=True)
    monkeypatch.setattr(tg_factory, "_route_channel_event", route)
    _corr(monkeypatch, None)

    await on_message(SimpleNamespace(message=tg._msg(text="too risky", reply_to=SimpleNamespace(message_id=44))), tg._context())

    assert adapter._handle_decision.await_args.kwargs["reason"] == "too risky"
    route.assert_not_awaited()


def test_an_ascii_telegram_mention_of_the_bot_counts_and_another_offset_does_not():
    ents = [{"type": "mention", "offset": 3, "length": 6}]
    assert _mentions_bot(ents, "hi @mybot", bot_id=1, bot_username="MyBot") is True
    assert _mentions_bot(ents, "hi there @mybot", bot_id=1, bot_username="MyBot") is False


async def test_a_telegram_text_message_reads_its_entities():
    router = tg._EventCaptureRouter()
    await tg_factory._route_channel_event(tg._bot_adapter(router), "p", tg._mention_msg("@MyBot hi", "mention", 0, 6))
    assert router.events[0].mentions_bot is True


async def test_a_discord_reject_modal_left_open_across_a_restart_is_decided_by_the_fallback(monkeypatch):
    adapter = dc._mock_adapter()
    adapter._handle_decision = AsyncMock(return_value=True)
    _, client = dc._install(monkeypatch, dc._FakeEntry({"100": adapter}))
    submit = dc._interaction(custom_id="rm:w:s:t", channel_id=100)
    submit.data["components"] = [{"components": [{"custom_id": "reason", "type": 4, "value": "too risky"}]}]

    await client.on_interaction(submit)

    adapter._handle_decision.assert_awaited_once()
    assert adapter._handle_decision.await_args.kwargs["reason"] == "too risky"


def _reject_view(ts: str = "111.2") -> dict:
    return {
        "private_metadata": f"reject:ws:sid:tc:C123:{ts}",
        "state": {"values": {"reason": {"reason_text": {"value": "no good"}}}},
    }


_APPROVAL_BLOCKS = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "tool: write_file"}},
    {"type": "section", "text": {"type": "mrkdwn", "text": "args: a.py"}},
    {"type": "actions", "elements": []},
]


async def test_a_rejected_top_level_slack_message_keeps_its_tool_and_args_blocks(monkeypatch):
    adapter = sl._mock_adapter()
    _, app = sl._install(monkeypatch, sl._FakeEntry({"C123": adapter}))
    client = SimpleNamespace(
        conversations_history=AsyncMock(return_value={"messages": [{"ts": "111.2", "blocks": _APPROVAL_BLOCKS}]}),
        conversations_replies=AsyncMock(return_value={"messages": []}),
        chat_update=AsyncMock(),
    )

    await app.views[slack_factory.REJECT_MODAL_CALLBACK_ID](AsyncMock(), {"user": {"id": "U1"}}, _reject_view(), client)

    blocks = client.chat_update.await_args.kwargs["blocks"]
    assert blocks[:2] == _APPROVAL_BLOCKS[:2] and blocks[2]["type"] == "context"


def _slack_raw(**payload) -> dict:
    return {"type": "message", "payload": {"channel": "C123", "user": "U1", "ts": "5.0", "text": "hi", **payload}}


async def test_slack_channel_and_thread_messages_keep_their_surfaces():
    n = SlackEventNormalizer(provider_id="p")
    channel = await n.normalize(_slack_raw())
    thread = await n.normalize(_slack_raw(thread_ts="1.0"))
    assert (channel.surface, channel.thread_anchor) == ("channel", None)
    assert (thread.surface, thread.thread_anchor) == ("thread", "1.0")
    assert mapping_anchor(channel) is None and mapping_anchor(thread) == "1.0"


async def test_slack_bot_messages_and_edits_are_still_filtered(monkeypatch):
    n = SlackEventNormalizer(provider_id="p")
    assert await n.normalize(_slack_raw(bot_id="B1")) is None
    assert await n.normalize(_slack_raw(subtype="message_changed")) is None
    assert (await n.normalize(_slack_raw(subtype="file_share"))) is not None

    adapter = sl._mock_adapter()
    _, app = sl._install(monkeypatch, sl._FakeEntry({"C123": adapter}))
    route = AsyncMock(return_value=True)
    monkeypatch.setattr(slack_factory, "_route_channel_event", route)
    for event in ({"bot_id": "B1"}, {"subtype": "message_changed"}, {"subtype": "channel_join"}):
        await app.events["message"]({"channel": "C123", "ts": "5.0", "user": "U1", **event}, SimpleNamespace(), SimpleNamespace())
    route.assert_not_awaited()


# ---- W36: an uncorrelated Telegram quote-reply is an ordinary message ------------------------------------------------


async def test_W36_an_uncorrelated_telegram_quote_reply_is_routed(monkeypatch):
    adapter = tg._mock_adapter()
    adapter.resolve_reply_target = MagicMock(return_value=None)
    _, on_message = tg._install(monkeypatch, tg._FakeEntry({"100": adapter}))
    route = AsyncMock(return_value=True)
    monkeypatch.setattr(tg_factory, "_route_channel_event", route)
    _corr(monkeypatch, None)
    msg = tg._msg(text="and also this", reply_to=SimpleNamespace(message_id=55))

    await on_message(SimpleNamespace(message=msg), tg._context())

    adapter._handle_decision.assert_not_awaited()
    route.assert_awaited_once()
    assert route.await_args.args[2] is msg


# ---- W56a: UTF-16 entity offsets; caption entities --------------------------------------------------------------------


def test_W56a_telegram_mention_offsets_are_utf16():
    # The emoji is 2 UTF-16 code units, so the mention entity is at offset 3, length 6.
    ents = [{"type": "mention", "offset": 3, "length": 6}]
    assert _mentions_bot(ents, "\U0001F44B @mybot hi", bot_id=1, bot_username="mybot") is True


async def test_W56a_a_caption_mention_of_the_bot_counts():
    router = tg._EventCaptureRouter()
    msg = tg._mention_msg(None, "mention", 0, 0)
    msg.entities = None
    msg.caption = "\U0001F44B @MyBot look"
    msg.caption_entities = [SimpleNamespace(type="mention", offset=3, length=6)]

    await tg_factory._route_channel_event(tg._bot_adapter(router), "p", msg)

    assert router.events[0].text == "\U0001F44B @MyBot look" and router.events[0].mentions_bot is True
