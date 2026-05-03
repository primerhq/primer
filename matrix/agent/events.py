"""Streaming-tap scaffolding for the agent executors.

Three things live here:

* :class:`AgentEventSubscriber` -- Protocol every tap subscriber
  satisfies. The executor fans :class:`matrix.model.chat.StreamEvent`
  events out to every registered subscriber concurrently.
* :class:`Subscription` -- handle the executor returns from
  ``subscribe``. Calling :meth:`unsubscribe` removes the subscriber
  from the fan-out list.
* :class:`_ExecutorToolResult` -- re-export of the chat-side synthetic
  event class. The actual definition lives in
  :mod:`matrix.model.chat` to keep the
  :data:`matrix.model.chat.ExtendedStreamContent` discriminated union
  self-contained (no chat -> agent module cycle).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

# Re-export for clarity at the agent-side import site. The actual
# class lives in matrix.model.chat to keep ExtendedStreamContent
# self-contained (no chat -> agent import cycle).
from matrix.model.chat import _ExecutorToolResult  # noqa: F401


if TYPE_CHECKING:
    from matrix.model.chat import StreamEvent


@runtime_checkable
class AgentEventSubscriber(Protocol):
    """Callback receiver for streaming-tap events.

    The executor calls :meth:`on_event` once per
    :class:`matrix.model.chat.StreamEvent`. Subscriber failures are
    logged and isolated -- they do NOT abort the agent's turn or
    affect other subscribers.
    """

    async def on_event(self, event: "StreamEvent") -> None: ...


class Subscription(BaseModel):
    """Handle returned from an executor's ``subscribe`` method.

    Calling :meth:`unsubscribe` removes the subscriber from the
    executor's fan-out list. The handle holds no live resource -- it
    is just a token for cancellation.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    subscription_id: str = Field(..., min_length=1)
    _executor: "object" = PrivateAttr()

    def __init__(
        self,
        *,
        subscription_id: str,
        _executor: "object",
    ) -> None:
        super().__init__(subscription_id=subscription_id)
        self._executor = _executor

    async def unsubscribe(self) -> None:
        await self._executor.unsubscribe(self)  # type: ignore[attr-defined]


__all__ = [
    "AgentEventSubscriber",
    "Subscription",
    "_ExecutorToolResult",
]
