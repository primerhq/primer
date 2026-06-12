"""Phase 1 sweep: model + capability + constraints + parser smoke together."""

from __future__ import annotations

from primer.channel.adapter import provider_supports_threads
from primer.channel.commands import ParsedCommand, parse_command
from primer.channel.constraints import (
    AssociationCounts,
    check_chat_association_allowed,
)
from primer.model.channel import ChannelProviderType, ChatChannelAssociation
from primer.model.chats import ChatChannelBinding


def test_phase1_surface_present():
    assert provider_supports_threads(ChannelProviderType.TELEGRAM) is False
    assert provider_supports_threads(ChannelProviderType.SLACK) is True
    assert parse_command("/switch chat-9") == ParsedCommand("switch", "chat-9")
    a = ChatChannelAssociation(channel_id="ch", default_agent_id="ag")
    assert a.relay_mode == "final"
    b = ChatChannelBinding(channel_id="ch")
    assert b.thread_external_id is None
    # single-type XOR: an empty channel allows a chat association
    check_chat_association_allowed(
        supports_threads=False, counts=AssociationCounts(0, 0))
