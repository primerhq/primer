from __future__ import annotations

import pytest

from primer.channel.slack.normalizer import SlackEventNormalizer
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import NormalizedEventType


@pytest.mark.asyncio
async def test_message_to_message_posted():
    normalizer = SlackEventNormalizer(provider_id="channel-provider-s")
    event = await normalizer.normalize(
        {
            "type": "message",
            "payload": {
                "channel": "C1",
                "user": "U1",
                "text": "hello",
                "ts": "111.1",
                "thread_ts": None,
            },
        }
    )
    assert event is not None
    assert event.type == NormalizedEventType.MESSAGE_POSTED
    assert event.provider == ChannelProviderType.SLACK
    assert event.surface == "channel"
    assert event.room_external_id == "C1"
    assert event.message_id == "111.1"
    assert event.thread_anchor is None
    assert event.mentions_bot is False
    assert event.sender.external_id == "U1"
    assert event.text == "hello"
    assert event.event_id == "111.1"


@pytest.mark.asyncio
async def test_app_mention_sets_mentions_bot():
    normalizer = SlackEventNormalizer(provider_id="x")
    event = await normalizer.normalize(
        {
            "type": "app_mention",
            "payload": {
                "channel": "C1",
                "user": "U1",
                "text": "<@bot> hi",
                "ts": "112.0",
            },
        }
    )
    assert event is not None
    assert event.type == NormalizedEventType.MESSAGE_POSTED
    assert event.mentions_bot is True


@pytest.mark.asyncio
async def test_thread_message_surface_thread():
    normalizer = SlackEventNormalizer(provider_id="x")
    event = await normalizer.normalize(
        {
            "type": "message",
            "payload": {
                "channel": "C1",
                "user": "U1",
                "text": "hi",
                "ts": "120.0",
                "thread_ts": "100.0",
            },
        }
    )
    assert event is not None
    assert event.surface == "thread"
    assert event.thread_anchor == "100.0"


@pytest.mark.asyncio
async def test_slash_command_to_command_invoked():
    normalizer = SlackEventNormalizer(provider_id="x")
    event = await normalizer.normalize(
        {
            "type": "slash_command",
            "payload": {
                "command": "/deploy",
                "text": "prod",
                "channel_id": "C1",
                "user_id": "U1",
                "trigger_id": "T9",
            },
        }
    )
    assert event is not None
    assert event.type == NormalizedEventType.COMMAND_INVOKED
    assert event.command == {"name": "deploy", "args": "prod"}
    assert event.room_external_id == "C1"


@pytest.mark.asyncio
async def test_bot_message_ignored():
    normalizer = SlackEventNormalizer(provider_id="x")
    event = await normalizer.normalize(
        {
            "type": "message",
            "payload": {
                "channel": "C1",
                "user": "U1",
                "text": "hi",
                "ts": "130.0",
                "bot_id": "B1",
            },
        }
    )
    assert event is None


@pytest.mark.asyncio
async def test_edit_subtype_ignored():
    normalizer = SlackEventNormalizer(provider_id="x")
    event = await normalizer.normalize(
        {
            "type": "message",
            "payload": {
                "subtype": "message_changed",
                "channel": "C1",
                "user": "U1",
                "ts": "140.0",
            },
        }
    )
    assert event is None


@pytest.mark.asyncio
async def test_file_share_subtype_kept():
    normalizer = SlackEventNormalizer(provider_id="x")
    event = await normalizer.normalize(
        {
            "type": "message",
            "payload": {
                "subtype": "file_share",
                "channel": "C1",
                "user": "U1",
                "ts": "113.0",
            },
        }
    )
    assert event is not None
    assert event.type == NormalizedEventType.MESSAGE_POSTED


@pytest.mark.asyncio
async def test_unknown_type_ignored():
    normalizer = SlackEventNormalizer(provider_id="x")
    event = await normalizer.normalize({"type": "reaction_added", "payload": {}})
    assert event is None


@pytest.mark.asyncio
async def test_capabilities_declares_core_types():
    caps = SlackEventNormalizer(provider_id="x").capabilities()
    assert caps.supported == {
        NormalizedEventType.MESSAGE_POSTED,
        NormalizedEventType.COMMAND_INVOKED,
    }
