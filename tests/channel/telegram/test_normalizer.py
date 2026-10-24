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


# --------------------------------------------------------------------------- #
# mentions_bot: a mention entity of the bot username, or a text_mention of
# the bot user. The bot identity (bot_id / bot_username) is what the factory
# read off the initialized application; a missing identity is False.
# --------------------------------------------------------------------------- #
def _tg_payload(**over) -> dict:
    p = {
        "message_id": 50,
        "chat": {"id": 555, "type": "supergroup"},
        "from": {"id": 42, "full_name": "Ada"},
        "text": "hi",
        "entities": [],
    }
    p.update(over)
    return p


@pytest.mark.asyncio
async def test_mention_entity_of_the_bot_username_sets_mentions_bot():
    # Case-insensitive: the message says @mybot, the bot's username is
    # MyBot.
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize({
        "type": "message",
        "payload": _tg_payload(
            text="hi @mybot",
            entities=[{"type": "mention", "offset": 3, "length": 6}],
            bot_id=777,
            bot_username="MyBot",
        ),
    })
    assert ev is not None
    assert ev.mentions_bot is True


@pytest.mark.asyncio
async def test_mention_entity_of_another_user_does_not():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize({
        "type": "message",
        "payload": _tg_payload(
            text="hi @other",
            entities=[{"type": "mention", "offset": 3, "length": 6}],
            bot_id=777,
            bot_username="MyBot",
        ),
    })
    assert ev is not None
    assert ev.mentions_bot is False


@pytest.mark.asyncio
async def test_bot_command_addressing_the_bot_is_not_a_mention():
    # /cmd@mybot carries a bot_command entity, not a mention entity: the
    # @botname suffix of the command does not count.
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize({
        "type": "message",
        "payload": _tg_payload(
            text="/cmd@mybot",
            entities=[{"type": "bot_command", "offset": 0, "length": 10}],
            bot_id=777,
            bot_username="MyBot",
        ),
    })
    assert ev is not None
    assert ev.type == NormalizedEventType.COMMAND_INVOKED
    assert ev.mentions_bot is False


@pytest.mark.asyncio
async def test_text_mention_of_the_bot_user_sets_mentions_bot():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize({
        "type": "message",
        "payload": _tg_payload(
            text="hi there",
            entities=[{
                "type": "text_mention", "offset": 3, "length": 5,
                "user": {"id": 777, "first_name": "MyBot"},
            }],
            bot_id=777,
            bot_username="MyBot",
        ),
    })
    assert ev is not None
    assert ev.mentions_bot is True


@pytest.mark.asyncio
async def test_text_mention_of_another_user_does_not():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize({
        "type": "message",
        "payload": _tg_payload(
            text="hi there",
            entities=[{
                "type": "text_mention", "offset": 3, "length": 5,
                "user": {"id": 999, "first_name": "Someone"},
            }],
            bot_id=777,
            bot_username="MyBot",
        ),
    })
    assert ev is not None
    assert ev.mentions_bot is False


@pytest.mark.asyncio
async def test_mention_without_bot_identity_is_not_a_mention():
    # No bot username (and no id to fall back on): nothing can match.
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize({
        "type": "message",
        "payload": _tg_payload(
            text="hi @mybot",
            entities=[{"type": "mention", "offset": 3, "length": 6}],
        ),
    })
    assert ev is not None
    assert ev.mentions_bot is False


@pytest.mark.asyncio
async def test_message_without_entities_defaults_to_false():
    n = TelegramEventNormalizer(provider_id="x")
    ev = await n.normalize({
        "type": "message",
        "payload": _tg_payload(bot_id=777, bot_username="MyBot"),
    })
    assert ev is not None
    assert ev.mentions_bot is False


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
