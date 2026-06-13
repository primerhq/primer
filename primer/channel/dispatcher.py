"""Fan-out dispatch from one parked session to its channels."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from primer.channel.adapter import PromptEnvelope


if TYPE_CHECKING:
    from primer.api.registries.channel_registry import ChannelRegistry


logger = logging.getLogger(__name__)


class ChannelDispatcher:
    """Fans envelopes out to every channel associated with a workspace."""

    def __init__(self, *, registry: "ChannelRegistry") -> None:
        self._registry = registry

    async def dispatch_prompt(
        self, *, envelope: PromptEnvelope,
    ) -> list[dict]:
        adapters = await self._registry.for_workspace(envelope.workspace_id)
        if not adapters:
            return []

        async def _one(adapter) -> dict:
            try:
                return await adapter.post_prompt(envelope)
            except Exception as exc:
                logger.warning(
                    "channel dispatcher: adapter raised on %s/%s: %s",
                    envelope.kind, envelope.tool_call_id, exc,
                )
                return {"error": str(exc)}

        results = await asyncio.gather(
            *[_one(a) for a in adapters],
            return_exceptions=False,
        )
        return list(results)


__all__ = ["ChannelDispatcher"]
