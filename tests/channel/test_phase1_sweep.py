"""Phase 1 sweep: model + capability + parser smoke together."""

from __future__ import annotations

from primer.channel.adapter import provider_supports_threads
from primer.channel.commands import ParsedCommand, parse_command
from primer.model.channel import ChannelProviderType
from primer.model.chats import ChatChannelBinding


def test_phase1_surface_present():
    assert provider_supports_threads(ChannelProviderType.TELEGRAM) is False
    assert provider_supports_threads(ChannelProviderType.SLACK) is True
    assert parse_command("/switch chat-9") == ParsedCommand("switch", "chat-9")
    b = ChatChannelBinding(channel_id="ch")
    assert b.thread_external_id is None
