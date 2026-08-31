"""session_append subscription dispatcher (S6 section 3).

The subscription names an EXISTING session; the rendered payload becomes a
user message on it. Everything about ordering lives in
:func:`primer.session.steer_delivery.deliver_steer`, which applies the S1
routing rule (queue behind an open turn, never a second USER_INPUT); this
module only maps the delivery outcome onto the dispatcher result envelope.
"""

from __future__ import annotations

import logging

from primer.model.trigger import Subscription
from primer.session.steer_delivery import (
    DELIVERED_MISSING,
    DELIVERED_SKIPPED_BUSY,
    deliver_steer,
)
from primer.trigger.subscribers import (
    DispatchDeps,
    SubscriptionDispatchResult,
    register,
)

logger = logging.getLogger(__name__)


class SessionAppendDispatcher:
    """Dispatcher for ``session_append`` subscriptions."""

    kind = "session_append"

    async def dispatch(
        self,
        sub: Subscription,
        *,
        rendered_payload: str,
        fire_context: dict,
        fire_id: str,
        deps: DispatchDeps,
    ) -> SubscriptionDispatchResult:
        if deps.workspace_registry is None:
            return SubscriptionDispatchResult(
                ok=False,
                error_code="dispatch_failed",
                error_message=(
                    "session_append requires a workspace_registry to reach "
                    "the target session's on-disk slot; the fire path did "
                    "not thread one"
                ),
            )
        try:
            delivery = await deliver_steer(
                session_id=sub.config.session_id,
                text=rendered_payload,
                parallelism=sub.parallelism,
                storage_provider=deps.storage_provider,
                scheduler=deps.scheduler,
                claim_engine=deps.claim_engine,
                workspace_registry=deps.workspace_registry,
                event_bus=deps.event_bus,
            )
        except Exception as exc:  # noqa: BLE001 - defensive perimeter
            return SubscriptionDispatchResult(
                ok=False,
                error_code="dispatch_failed",
                error_message=str(exc),
            )
        if delivery.outcome == DELIVERED_MISSING:
            return SubscriptionDispatchResult(
                ok=True,
                skipped=True,
                error_code="skipped_session_missing",
                error_message=(
                    f"session {sub.config.session_id!r} no longer exists"
                ),
            )
        if delivery.outcome == DELIVERED_SKIPPED_BUSY:
            return SubscriptionDispatchResult(
                ok=True,
                skipped=True,
                error_code="skipped_session_busy",
                error_message=(
                    f"session {sub.config.session_id!r} has a turn in flight"
                ),
            )
        return SubscriptionDispatchResult(
            ok=True, artefact_id=delivery.session_id,
        )


register("session_append", SessionAppendDispatcher())


__all__ = ["SessionAppendDispatcher"]
