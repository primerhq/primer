from __future__ import annotations

import pytest

from primer.channel.discord.normalizer import DiscordEventNormalizer
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import NormalizedEventType


@pytest.mark.asyncio
async def test_text_channel_message():
    normalizer = DiscordEventNormalizer(provider_id="channel-provider-d")
    event = await normalizer.normalize(
        {
            "type": "message",
            "payload": {
                "id": 900,
                "content": "hello",
                "author": {
                    "id": 42,
                    "name": "ada",
                    "display_name": "Ada",
                    "bot": False,
                },
                "channel": {"id": 700, "kind": "text", "parent_id": None},
            },
        }
    )
    assert event is not None
    assert event.type == NormalizedEventType.MESSAGE_POSTED
    assert event.provider == ChannelProviderType.DISCORD
    assert event.surface == "channel"
    assert event.room_external_id == "700"
    assert event.message_id == "900"
    assert event.thread_anchor is None
    assert event.sender.external_id == "42"
    assert event.sender.display_name == "Ada"
    assert event.text == "hello"
    assert event.sender.is_bot is False


@pytest.mark.asyncio
async def test_thread_message_surface_thread():
    normalizer = DiscordEventNormalizer(provider_id="channel-provider-d")
    event = await normalizer.normalize(
        {
            "type": "message",
            "payload": {
                "id": 901,
                "content": "in thread",
                "author": {
                    "id": 42,
                    "name": "ada",
                    "display_name": "Ada",
                    "bot": False,
                },
                "channel": {"id": 701, "kind": "thread", "parent_id": 700},
            },
        }
    )
    assert event is not None
    assert event.surface == "thread"
    assert event.thread_anchor == "701"
    assert event.room_external_id == "700"


@pytest.mark.asyncio
async def test_dm_message_surface_dm():
    normalizer = DiscordEventNormalizer(provider_id="channel-provider-d")
    event = await normalizer.normalize(
        {
            "type": "message",
            "payload": {
                "id": 902,
                "content": "dm",
                "author": {
                    "id": 42,
                    "name": "ada",
                    "display_name": "Ada",
                    "bot": False,
                },
                "channel": {"id": 800, "kind": "dm", "parent_id": None},
            },
        }
    )
    assert event is not None
    assert event.surface == "dm"
    assert event.room_external_id == "800"


@pytest.mark.asyncio
async def test_bot_author_ignored():
    normalizer = DiscordEventNormalizer(provider_id="channel-provider-d")
    event = await normalizer.normalize(
        {
            "type": "message",
            "payload": {
                "id": 903,
                "content": "beep",
                "author": {
                    "id": 99,
                    "name": "robot",
                    "display_name": "Robot",
                    "bot": True,
                },
                "channel": {"id": 700, "kind": "text", "parent_id": None},
            },
        }
    )
    assert event is None


@pytest.mark.asyncio
async def test_application_command_to_command_invoked():
    normalizer = DiscordEventNormalizer(provider_id="channel-provider-d")
    event = await normalizer.normalize(
        {
            "type": "application_command",
            "payload": {
                "name": "deploy",
                "options": {"env": "prod"},
                "interaction_id": "I1",
                "user": {"id": 42},
                "channel": {"id": 701, "kind": "thread", "parent_id": 700},
            },
        }
    )
    assert event is not None
    assert event.type == NormalizedEventType.COMMAND_INVOKED
    assert event.command == {"name": "deploy", "args": {"env": "prod"}}
    assert event.surface == "thread"


@pytest.mark.asyncio
async def test_unknown_type_ignored():
    normalizer = DiscordEventNormalizer(provider_id="channel-provider-d")
    event = await normalizer.normalize(
        {"type": "raw_reaction_add", "payload": {}}
    )
    assert event is None


@pytest.mark.asyncio
async def test_capabilities_declares_core_types():
    normalizer = DiscordEventNormalizer(provider_id="channel-provider-d")
    caps = normalizer.capabilities()
    assert caps.supported == {
        NormalizedEventType.MESSAGE_POSTED,
        NormalizedEventType.COMMAND_INVOKED,
        NormalizedEventType.COMPONENT_ACTED,
    }
