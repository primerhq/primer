"""Channel adapter ABC + provider-agnostic envelope types."""

from __future__ import annotations

from abc import ABC, abstractmethod
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
