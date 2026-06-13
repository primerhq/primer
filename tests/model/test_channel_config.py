import pytest
from primer.model.channel import (
    Channel, ChannelProviderType, SlackChannelConfig, TelegramChannelConfig,
    ChatConfig,
)

def test_channel_carries_chat_config():
    ch = Channel(id="ch-1", provider_id="cp-1", external_id="C123",
                 provider=ChannelProviderType.SLACK,
                 config=SlackChannelConfig(chats=ChatConfig(
                     enabled=True, default_agent="agent-x")))
    assert ch.config.chats.enabled is True
    assert ch.config.chats.default_agent == "agent-x"
    assert ch.config.chats.relay_mode == "final"

def test_default_agent_must_be_in_allowed_agents():
    with pytest.raises(ValueError):
        ChatConfig(enabled=True, default_agent="agent-z",
                   allowed_agents=["agent-a", "agent-b"])

def test_empty_allowed_agents_allows_any_default():
    cfg = ChatConfig(enabled=True, default_agent="agent-z", allowed_agents=[])
    assert cfg.default_agent == "agent-z"

def test_config_must_match_provider():
    with pytest.raises(ValueError):
        Channel(id="ch-1", provider_id="cp-1", external_id="C1",
                provider=ChannelProviderType.TELEGRAM,
                config=SlackChannelConfig(chats=ChatConfig()))
