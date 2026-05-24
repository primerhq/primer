"""Inbound side of the channels subsystem."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from matrix.channel.adapter import ResponseEnvelope
from matrix.model.except_ import BadRequestError


if TYPE_CHECKING:
    from matrix.int.event_bus import EventBus


logger = logging.getLogger(__name__)


class ChannelInbox:
    """Single fan-in point for every adapter's inbound responses."""

    def __init__(self, *, event_bus: "EventBus") -> None:
        self._event_bus = event_bus

    async def handle_response(self, env: ResponseEnvelope) -> None:
        if env.kind == "ask_user":
            event_key = f"ask_user:{env.session_id}:{env.tool_call_id}"
            payload: dict = {"response": env.response}
        elif env.kind == "tool_approval":
            event_key = f"tool_approval:{env.session_id}:{env.tool_call_id}"
            payload = {"decision": env.decision, "reason": env.reason}
        else:
            raise BadRequestError(
                f"unknown ResponseEnvelope kind {env.kind!r}"
            )
        logger.info(
            "channel inbox publishing %s for session=%s tool_call=%s",
            env.kind, env.session_id, env.tool_call_id,
        )
        await self._event_bus.publish(event_key, payload)


__all__ = ["ChannelInbox"]
