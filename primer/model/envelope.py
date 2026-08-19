"""Provider-agnostic prompt/response envelopes.

These are the channel-neutral payloads the agent runtime hands to any
delivery surface (channel adapters, console). They live in core model
so the agent/worker layers never import primer.channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptEnvelope:
    """Provider-agnostic ask-user / approval payload."""

    kind: str
    workspace_id: str
    session_id: str
    tool_call_id: str
    prompt: str
    response_schema: dict[str, Any] | None
    choices: list[str] | None
    timeout_at_iso: str | None
    # Structured approval detail (kind == "tool_approval"), so renderers can
    # format the call cleanly instead of parsing it out of ``prompt``.
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    # Artifact-backed media parts (as dicts) to upload alongside the prompt,
    # e.g. workspace files attached to ask_user / inform_user. None == no media.
    media: list[dict[str, Any]] | None = None
    # Optional attribution context surfaced in channel gate posts.
    workspace_name: str | None = None
    session_label: str | None = None


@dataclass
class ResponseEnvelope:
    """Provider-agnostic response from the platform."""

    kind: str
    workspace_id: str
    session_id: str
    tool_call_id: str
    response: Any
    decision: str | None
    reason: str | None
    platform_metadata: dict[str, Any] = field(default_factory=dict)


RELAY_EVERY_TURN_KEY = "relay_every_turn"
"""``WorkspaceSession.metadata`` flag: relay after EVERY drained turn.

Set by the channel thread mapper when the source trigger is interactive
(S6 section 4). Lives in core model so the worker turn loop reads it
without importing the optional ``primer.channel`` package.
"""


__all__ = ["RELAY_EVERY_TURN_KEY", "PromptEnvelope", "ResponseEnvelope"]
