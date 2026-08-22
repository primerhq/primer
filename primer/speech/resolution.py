"""Resolve which voice a synthesis call should use.

Agents stay text-in / text-out: speech is an edge transform, so nothing
here reaches into the agent runtime. The only agent-level knob is
``Agent.tts_voice``, and this function is the single place that decides
how it composes with the install-wide default.
"""

from __future__ import annotations


def resolve_tts_voice(
    *,
    agent_tts_voice: str | None,
    active_voice: str | None,
    provider_default_voice: str | None,
) -> str | None:
    """Pick a voice: agent override, then install default, then the row.

    Empty strings are treated as unset so a blank form field cannot
    shadow a real default.
    """
    for candidate in (agent_tts_voice, active_voice, provider_default_voice):
        if candidate:
            return candidate
    return None


__all__ = ["resolve_tts_voice"]
