"""Channel adapter ABC + provider-agnostic envelope types."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


def session_thread_label(session_id: str) -> str:
    """Human-facing title for a per-session conversation thread.

    Channels that support threads (Slack, Discord) anchor one thread per agent
    session and route every prompt (ask_user + tool approvals) into it.
    """
    return f"Agent session {session_id}"


def format_tool_args(tool_args: dict[str, Any] | None) -> str:
    """Pretty-print tool-call arguments as JSON for channel rendering.

    Channels show this inside a code block instead of dumping the raw
    ``repr`` of the dict. Falls back to ``str`` if the args are not
    JSON-serialisable.
    """
    if not tool_args:
        return "{}"
    try:
        return json.dumps(tool_args, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(tool_args)


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


class ChannelAdapter(ABC):
    """Per-channel adapter instance."""

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

    @abstractmethod
    async def verify(self) -> None:
        """Smoke-test the credential set + target id. Raises on failure."""

    @abstractmethod
    async def post_prompt(
        self, envelope: PromptEnvelope,
    ) -> dict[str, Any]:
        """Render and post the envelope."""


__all__ = [
    "ChannelAdapter",
    "PromptEnvelope",
    "ResponseEnvelope",
]
