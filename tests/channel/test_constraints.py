import pytest
from primer.channel.constraints import validate_chat_config
from primer.model.channel import ChatConfig

def test_validate_chat_config_ok():
    validate_chat_config(ChatConfig(enabled=True, default_agent="a"))  # no raise

def test_chatconfig_rejects_default_not_in_allowed():
    with pytest.raises(ValueError):
        ChatConfig(enabled=True, default_agent="z", allowed_agents=["a"])

def test_association_models_removed():
    import primer.model.channel as m
    assert not hasattr(m, "ChatChannelAssociation")
    assert not hasattr(m, "WorkspaceChannelAssociation")
