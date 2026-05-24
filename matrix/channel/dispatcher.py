"""Fan-out dispatch from one parked session to its channels."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from matrix.channel.adapter import PromptEnvelope


if TYPE_CHECKING:
    from matrix.api.registries.channel_registry import ChannelRegistry


logger = logging.getLogger(__name__)


class ChannelDispatcher:
    """Fans envelopes out to every channel associated with a workspace."""

    def __init__(self, *, registry: "ChannelRegistry") -> None:
        self._registry = registry

    async def dispatch_prompt(
        self, *, envelope: PromptEnvelope,
    ) -> list[dict]:
        pairs = await self._registry.for_workspace(envelope.workspace_id)
        forward_key = (
            "forward_ask_user"
            if envelope.kind == "ask_user"
            else "forward_tool_approval"
        )
        eligible = [
            (adapter, assoc) for adapter, assoc in pairs
            if _flag(assoc, forward_key)
        ]
        if not eligible:
            return []

        async def _one(adapter, _assoc) -> dict:
            try:
                return await adapter.post_prompt(envelope)
            except Exception as exc:
                logger.warning(
                    "channel dispatcher: adapter raised on %s/%s: %s",
                    envelope.kind, envelope.tool_call_id, exc,
                )
                return {"error": str(exc)}

        results = await asyncio.gather(
            *[_one(a, assoc) for a, assoc in eligible],
            return_exceptions=False,
        )
        return list(results)


def _flag(assoc, name: str) -> bool:
    """Read a boolean flag from a Pydantic row OR a dict (test stub)."""
    if isinstance(assoc, dict):
        return bool(assoc.get(name))
    return bool(getattr(assoc, name, None))


__all__ = ["ChannelDispatcher"]
