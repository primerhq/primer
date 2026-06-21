import pytest

from primer.channel.telegram.normalizer import TelegramEventNormalizer
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import NormalizedEventType


@pytest.mark.asyncio
async def test_private_text_message():
    n = TelegramEventNormalizer(provider_id="channel-provider-t")
    ev = await n.normalize(
        {
            "type": "message",
            "payload": {
                "message_id": 7,
                "chat": {"id": 555, "type": "private"},
                "from": {"id": 42, "full_name": "Ada"},
                "text": "hello",
            },
        }
    )
    assert ev is not None
    assert ev.type == NormalizedEventType.MESSAGE_POSTED
    assert ev.provider == ChannelProviderType.TELEGRAM
    assert ev.surface == "dm"
    assert ev.room_external_id == "555"
    assert ev.message_id == "7"
    assert ev.sender.external_id == "42"
    assert ev.sender.display_name == "Ada"
    assert ev.text == "hello"
    assert ev.thread_anchor is None


@pytest.mark.asyncio
async def test_group_message_surface_channel():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize(
        {
            "type": "message",
            "payload": {
                "message_id": 8,
                "chat": {"id": 555, "type": "supergroup"},
                "from": {"id": 42, "full_name": "Ada"},
                "text": "hello",
            },
        }
    )
    assert ev is not None
    assert ev.surface == "channel"


@pytest.mark.asyncio
async def test_bot_command_entity_to_command_invoked():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize(
        {
            "type": "message",
            "payload": {
                "message_id": 9,
                "chat": {"id": 555, "type": "private"},
                "from": {"id": 42, "full_name": "Ada"},
                "text": "/deploy prod",
                "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
            },
        }
    )
    assert ev is not None
    assert ev.type == NormalizedEventType.COMMAND_INVOKED
    assert ev.command == {"name": "deploy", "args": "prod"}


@pytest.mark.asyncio
async def test_command_strips_bot_suffix():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize(
        {
            "type": "message",
            "payload": {
                "message_id": 10,
                "chat": {"id": 555, "type": "private"},
                "from": {"id": 42, "full_name": "Ada"},
                "text": "/deploy@mybot prod",
                "entities": [{"type": "bot_command", "offset": 0, "length": 13}],
            },
        }
    )
    assert ev is not None
    assert ev.command["name"] == "deploy"


@pytest.mark.asyncio
async def test_callback_query_to_component_acted():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize(
        {
            "type": "callback_query",
            "payload": {
                "id": "cq1",
                "data": "pick:x",
                "from": {"id": 9},
                "message": {"chat": {"id": 555, "type": "private"}},
            },
        }
    )
    assert ev is not None
    assert ev.type == NormalizedEventType.COMPONENT_ACTED
    assert ev.surface == "dm"
    assert ev.component == {"id": "cq1", "value": "pick:x"}


@pytest.mark.asyncio
async def test_media_message_stays_message_posted():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize(
        {
            "type": "message",
            "payload": {
                "message_id": 11,
                "chat": {"id": 555, "type": "private"},
                "from": {"id": 42, "full_name": "Ada"},
                "photo": [{"file_id": "ph1"}],
            },
        }
    )
    assert ev is not None
    assert ev.type == NormalizedEventType.MESSAGE_POSTED


@pytest.mark.asyncio
async def test_unknown_type_ignored():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize({"type": "edited_message", "payload": {}})
    assert ev is None


@pytest.mark.asyncio
async def test_capabilities_declares_core_types():
    caps = TelegramEventNormalizer(provider_id="x").capabilities()
    assert caps.supported == {
        NormalizedEventType.MESSAGE_POSTED,
        NormalizedEventType.COMMAND_INVOKED,
        NormalizedEventType.COMPONENT_ACTED,
    }
