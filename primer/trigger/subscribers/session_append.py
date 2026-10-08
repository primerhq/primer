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
from primer.model.workspace_session import WorkspaceSession
from primer.session.steer_delivery import (
    DELIVERED_MISSING,
    DELIVERED_SKIPPED_BUSY,
    deliver_steer,
)
from primer.trigger.owner import refuse_steer_above_fire
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
        # The steered session runs at its own initiator's rank: a fire
        # ranked below it must not drive it (security review A-20).
        target = await deps.storage_provider.get_storage(
            WorkspaceSession,
        ).get(sub.config.session_id)
        # A missing target skips the guard on purpose: deliver_steer finds the
        # same absence and reports DELIVERED_MISSING, mapped below to a
        # non-failing skip. Nothing is delivered either way, so there is no
        # rank to protect.
        if target is not None:
            reason = await refuse_steer_above_fire(
                sub, target, deps.storage_provider,
            )
            if reason is not None:
                return SubscriptionDispatchResult(
                    ok=False,
                    error_code="steer_outranks_fire",
                    error_message=reason,
                )
        try:
            delivery = await deliver_steer(
                session_id=sub.config.session_id,
                text=rendered_payload,
                parallelism=sub.parallelism,
                # 01a08c08: a trigger fires with nobody present. It must
                # not be the thing that silently clears an operator's
                # pause -- see wake_session's human_intent docstring.
                human_intent=False,
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
