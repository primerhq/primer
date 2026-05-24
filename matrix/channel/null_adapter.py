"""No-op adapter used by tests."""

from __future__ import annotations

from typing import Any

from matrix.channel.adapter import ChannelAdapter, PromptEnvelope


class NullChannelAdapter(ChannelAdapter):
    """Records every ``post_prompt`` call in ``posted``."""

    def __init__(self) -> None:
        self.posted: list[PromptEnvelope] = []
        self._closed = False

    async def initialize(self) -> None:
        self._closed = False

    async def aclose(self) -> None:
        self._closed = True

    async def verify(self) -> None:
        return None

    async def post_prompt(
        self, envelope: PromptEnvelope,
    ) -> dict[str, Any]:
        self.posted.append(envelope)
        return {"posted": True, "kind": envelope.kind}


__all__ = ["NullChannelAdapter"]
