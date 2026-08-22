"""emit_session_wake: the dual-delivery seam for durable wake signals.

Every migrated wake source calls this instead of a bare bus publish.
Two halves, in order:

1. Append a ``session.wake`` event to the platform log (durable). The
   dispatcher's flip sink replays it from the cursor even if every
   volatile delivery was lost.
2. Legacy-publish the raw ``event_key`` on the volatile bus
   (transport-fast; the YieldEventListener keeps sub-second wakes).

Guarded flips make the two deliveries racing each other a no-op, so
dual delivery is safe by construction. Publish failures are logged,
never raised: the event is the durable half.
"""

from __future__ import annotations

import logging
from typing import Any

from primer.events.recorder import recorder_for

logger = logging.getLogger(__name__)


async def emit_session_wake(
    storage_provider: Any,
    bus: Any,
    event_key: str,
    payload: dict[str, Any] | None = None,
) -> None:
    await recorder_for(storage_provider, bus).emit(
        "session.wake",
        payload={"event_key": event_key, "wake_payload": payload or {}},
    )
    if bus is None:
        return
    try:
        await bus.publish(event_key, payload or {})
    except Exception:  # noqa: BLE001 - the event is the durable half
        logger.warning(
            "session.wake legacy publish failed for %r; the dispatcher "
            "will deliver from the log", event_key, exc_info=True,
        )


__all__ = ["emit_session_wake"]
