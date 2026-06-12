"""ChatChannelAssociation model: defaults, id autogen, relay_mode literal."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.channel import ChatChannelAssociation


def test_defaults_and_id_autogen():
    a = ChatChannelAssociation(channel_id="ch-1", default_agent_id="agent-x")
    assert a.id.startswith("chat-channel-association-")
    assert a.channel_id == "ch-1"
    assert a.default_agent_id == "agent-x"
    assert a.enabled is True
    assert a.relay_mode == "final"
    assert a.forward_ask_user is True
    assert a.forward_tool_approval is True
    assert a.forward_inform is True
    assert a.active_chat_id is None


def test_relay_mode_accepts_all():
    a = ChatChannelAssociation(
        channel_id="ch-1", default_agent_id="agent-x", relay_mode="all",
    )
    assert a.relay_mode == "all"


def test_relay_mode_rejects_unknown():
    with pytest.raises(ValidationError):
        ChatChannelAssociation(
            channel_id="ch-1", default_agent_id="agent-x", relay_mode="streaming",
        )


def test_channel_id_required_nonempty():
    with pytest.raises(ValidationError):
        ChatChannelAssociation(channel_id="", default_agent_id="agent-x")
