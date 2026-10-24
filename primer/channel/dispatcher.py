"""Fan-out dispatch from one parked session to its channels."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
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
        self, *, envelope: PromptEnvelope, session=None,
    ) -> list[dict]:
        """Post ``envelope`` to the channel of the session's reply binding (the workspace's when there is no session).

        The binding's anchor goes on EVERY post (questions, approvals, ``inform`` and the final result) unless the envelope
        already names a thread: a session started from a platform thread keeps its whole conversation in that thread, and a
        reply binding set with an anchor posts there (ticket 01a16e37-b98e items 4 and 5). Before, only the final result
        carried it, and every other post opened a thread of its own. With no anchor the adapter opens or reuses its
        per-session thread (Slack, Discord) or posts into the room (Telegram, which has no threads and ignores an anchor).
        """
        if session is not None:
            binding, adapters = await self._registry.route_for_session(session)
        else:
            binding, adapters = await self._registry.route_for_workspace(envelope.workspace_id)
        if not adapters:
            return []
        anchor = getattr(binding, "anchor", None)
        if anchor and not envelope.thread_anchor:
            envelope = replace(envelope, thread_anchor=anchor)

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
