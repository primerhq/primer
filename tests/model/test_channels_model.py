"""Validation tests for the Channels entity model."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from primer.model.channel import (
    Channel,
    ChannelProvider,
    ChannelProviderType,
    ChatConfig,
    DiscordChannelConfig,
    DiscordChannelProviderConfig,
    SlackChannelConfig,
    SlackChannelProviderConfig,
    TelegramChannelConfig,
    TelegramChannelProviderConfig,
)


def _valid_slack_config() -> SlackChannelProviderConfig:
    return SlackChannelProviderConfig(
        app_token=SecretStr("xapp-test"),
        bot_token=SecretStr("xoxb-test"),
    )


def test_channel_provider_type_values_stable():
    assert ChannelProviderType.SLACK.value == "slack"
    assert ChannelProviderType.TELEGRAM.value == "telegram"
    assert ChannelProviderType.DISCORD.value == "discord"


def test_provider_row_discriminator_slack():
    row = ChannelProvider(
        id="cp-1",
        provider=ChannelProviderType.SLACK,
        config=_valid_slack_config(),
    )
    assert row.provider == ChannelProviderType.SLACK
    assert isinstance(row.config, SlackChannelProviderConfig)


def test_provider_row_discriminator_mismatch_rejected():
    with pytest.raises(ValidationError):
        ChannelProvider(
            id="cp-2",
            provider=ChannelProviderType.SLACK,
            config=TelegramChannelProviderConfig(
                bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456"),
            ),
        )


def test_channel_minimal_fields():
    c = Channel(id="ch-1", provider_id="cp-1", provider=ChannelProviderType.SLACK, external_id="C0123")
    assert c.label is None
    assert isinstance(c.config, SlackChannelConfig)


def test_channel_external_id_is_required_field():
    """external_id is a required field; omitting it raises ValidationError."""
    with pytest.raises(ValidationError):
        Channel(id="ch-2", provider_id="cp-1", provider=ChannelProviderType.SLACK)


def test_channel_provider_is_required():
    with pytest.raises((ValidationError, TypeError)):
        Channel(id="ch-3", provider_id="cp-1", external_id="C0001")


def test_channel_config_defaults_to_empty_for_provider():
    slack_ch = Channel(id="ch-s", provider_id="cp-1", provider=ChannelProviderType.SLACK, external_id="C1")
    assert isinstance(slack_ch.config, SlackChannelConfig)
    assert slack_ch.config.chats.enabled is False

    tg_ch = Channel(id="ch-t", provider_id="cp-1", provider=ChannelProviderType.TELEGRAM, external_id="123")
    assert isinstance(tg_ch.config, TelegramChannelConfig)

    dc_ch = Channel(id="ch-d", provider_id="cp-1", provider=ChannelProviderType.DISCORD, external_id="99999")
    assert isinstance(dc_ch.config, DiscordChannelConfig)


def test_channel_config_mismatch_rejected():
    """A Slack channel with a Telegram config must be rejected."""
    with pytest.raises(ValidationError):
        Channel(
            id="ch-bad",
            provider_id="cp-1",
            provider=ChannelProviderType.SLACK,
            external_id="C1",
            config=TelegramChannelConfig(),
        )


def test_chat_config_defaults():
    cfg = ChatConfig()
    assert cfg.enabled is False
    assert cfg.relay_mode == "final"








def test_channel_config_chats_enabled_roundtrip():
    ch = Channel(
        id="ch-cfg",
        provider_id="cp-1",
        provider=ChannelProviderType.SLACK,
        external_id="C-cfg",
        config=SlackChannelConfig(
            chats=ChatConfig(enabled=True)
        ),
    )
    assert ch.config.chats.enabled is True


def test_slack_config_requires_xapp_prefix():
    with pytest.raises(ValidationError):
        SlackChannelProviderConfig(
            app_token=SecretStr("nope"),
            bot_token=SecretStr("xoxb-abc"),
        )


def test_slack_config_requires_xoxb_prefix():
    with pytest.raises(ValidationError):
        SlackChannelProviderConfig(
            app_token=SecretStr("xapp-abc"),
            bot_token=SecretStr("xowrong-abc"),
        )


def test_slack_config_accepts_valid_token_prefixes():
    c = SlackChannelProviderConfig(
        app_token=SecretStr("xapp-1-A1B2-1234567890-abc"),
        bot_token=SecretStr("xoxb-1234567890-abc"),
    )
    assert c.signing_secret is None


def test_telegram_config_requires_token_shape():
    import pytest
    from pydantic import SecretStr, ValidationError
    from primer.model.channel import TelegramChannelProviderConfig
    with pytest.raises(ValidationError):
        TelegramChannelProviderConfig(bot_token=SecretStr("short"))
    TelegramChannelProviderConfig(
        bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456"),
    )


def test_telegram_poll_timeout_bounds():
    import pytest
    from pydantic import SecretStr, ValidationError
    from primer.model.channel import TelegramChannelProviderConfig
    with pytest.raises(ValidationError):
        TelegramChannelProviderConfig(
            bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyz123456"),
            poll_timeout_seconds=100,
        )


def test_discord_config_requires_long_token():
    import pytest
    from pydantic import SecretStr, ValidationError
    with pytest.raises(ValidationError):
        DiscordChannelProviderConfig(bot_token=SecretStr("tiny"))
    cfg = DiscordChannelProviderConfig(
        bot_token=SecretStr("a" * 60),
    )
    assert cfg.enable_dms is True


def test_discord_config_enable_dms_toggles():
    from pydantic import SecretStr
    cfg = DiscordChannelProviderConfig(
        bot_token=SecretStr("a" * 60), enable_dms=False,
    )
    assert cfg.enable_dms is False


def test_the_served_mask_passes_the_token_rules_for_the_restore_and_nothing_else_does():
    """Audit G2-01: the exact mask a GET serves passes each token validator (the update restores the stored token and then
    judges it); a tail-form mask or any other non-token is still refused."""
    from primer.common.url_userinfo import MASK
    from primer.model.channel import TelegramChannelProviderConfig

    assert SlackChannelProviderConfig(app_token=SecretStr(MASK), bot_token=SecretStr(MASK)).app_token.get_secret_value() == MASK
    assert TelegramChannelProviderConfig(bot_token=SecretStr(MASK)).bot_token.get_secret_value() == MASK
    assert DiscordChannelProviderConfig(bot_token=SecretStr(MASK)).bot_token.get_secret_value() == MASK
    for near in (MASK + "abcd", MASK[:-1], "x" + MASK):
        with pytest.raises(ValidationError):
            SlackChannelProviderConfig(app_token=SecretStr(near), bot_token=SecretStr("xoxb-ok"))
        with pytest.raises(ValidationError):
            TelegramChannelProviderConfig(bot_token=SecretStr(near))
        with pytest.raises(ValidationError):
            DiscordChannelProviderConfig(bot_token=SecretStr(near))


def test_channel_token_problem_judges_a_bare_mask_by_the_platform_rule():
    """Ticket 01a16b64 R5: an unrestored mask never reaches ``channel_token_problem`` (``check_channel_provider_secrets`` refuses a
    served mask first), so it has no mask branch of its own. A bare mask passed to it directly still breaks every platform's rule."""
    from primer.common.url_userinfo import MASK
    from primer.model.channel import TelegramChannelProviderConfig, channel_token_problem

    assert channel_token_problem(SlackChannelProviderConfig(app_token=SecretStr("xapp-1"), bot_token=SecretStr(MASK))) == (
        "bot_token", "bot_token must start with 'xoxb-' (Slack bot token)",
    )
    assert channel_token_problem(SlackChannelProviderConfig(app_token=SecretStr(MASK), bot_token=SecretStr("xoxb-1"))) == (
        "app_token", "app_token must start with 'xapp-' (Slack app-level token)",
    )
    assert channel_token_problem(TelegramChannelProviderConfig(bot_token=SecretStr(MASK))) == (
        "bot_token", "bot_token must be in the '123456:ABCDEF...' shape",
    )
    assert channel_token_problem(DiscordChannelProviderConfig(bot_token=SecretStr(MASK)))[0] == "bot_token"
    assert channel_token_problem(SlackChannelProviderConfig(app_token=SecretStr("xapp-1"), bot_token=SecretStr("xoxb-1"))) is None
    assert channel_token_problem(DiscordChannelProviderConfig(bot_token=SecretStr("a" * 40))) is None
