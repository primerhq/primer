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
    # Platform thread this envelope must post into (S6 section 5). Set from
    # the session's reply binding when the session is thread-mapped; None
    # lets the adapter open or reuse its own per-session thread.
    thread_anchor: str | None = None
    # The id of the gate this prompt asks about (resume_metadata["gate_id"], C-033): a platform keeps it with the button or message it posts and
    # brings it back on the click, so a decision for a gate that has since been replaced under the same tool_call_id is refused. None for a park
    # from before gates had ids and for informs.
    gate_id: str | None = None


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
    # The gate id (or its first 12 characters, from a platform with a tight limit) the click or reply carried back; None for a button posted before
    # gates had ids, and for a reply that answers whatever is pending in a thread.
    gate_id: str | None = None


RELAY_EVERY_TURN_KEY = "relay_every_turn"
"""``WorkspaceSession.metadata`` flag: relay after EVERY drained turn.

Set by the channel thread mapper when the source trigger is interactive
(S6 section 4). Lives in core model so the worker turn loop reads it
without importing the optional ``primer.channel`` package.
"""


__all__ = ["RELAY_EVERY_TURN_KEY", "PromptEnvelope", "ResponseEnvelope"]
