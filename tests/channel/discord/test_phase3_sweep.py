"""Phase 3 sweep: discord chat-inbound + commands + autocomplete import."""

from __future__ import annotations

from primer.channel.discord.commands import (
    agent_autocomplete_choices, handle_app_command,
)


def test_phase3_surface_present():
    assert callable(agent_autocomplete_choices)
    assert callable(handle_app_command)
