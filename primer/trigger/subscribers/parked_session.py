"""parked_session subscription dispatcher — Spec §5.4.

A ``parked_session`` subscription is the dynamic one created by the
``subscribe_to_trigger`` yielding tool (Phase 8.2 work) — the agent
yields with a reference to a one-off ``delayed`` or ``scheduled``
trigger, the worker writes a Subscription pointing at the parked
session, and the trigger fire reaches back to wake the session up.

On fire:

1. Load the target session by ``sub.config.session_id``.
2. Verify it's still parked at the expected ``tool_call_id``. If not
   (session ended, resumed itself, parked on a different tool, etc.)
   delete the subscription and return a structured skip.
3. Build the tool result envelope (``{ok, fire_context, payload}``).
4. Publish the result onto the parked session's resume ``event_key``
   via the shared :func:`primer.session.yields.respond_to_yield`
   helper — same path the REST endpoints use.
5. Delete the subscription. ``parked_session`` is a one-shot;
   leaving the row would let a later scheduled fire wake a
   session that's already moved on.

This dispatcher needs an :class:`EventBus` on the deps to reach the
worker pool's resumable-flip listener. The fire orchestrator (Phase 6)
threads it through; the trigger pool wires it from the same singleton
the API uses.
"""

from __future__ import annotations

import json
import logging

from primer.model.trigger import Subscription
from primer.model.workspace_session import SessionStatus, WorkspaceSession
from primer.session.yields import RespondToYieldDeps, respond_to_yield
from primer.trigger.subscribers import (
    DispatchDeps,
    SubscriptionDispatchResult,
    register,
)


logger = logging.getLogger(__name__)


class ParkedSessionDispatcher:
    """Dispatcher for ``parked_session`` subscriptions."""

    kind = "parked_session"

    async def dispatch(
        self,
        sub: Subscription,
        *,
        rendered_payload: str,
        fire_context: dict,
        fire_id: str,
        deps: DispatchDeps,
    ) -> SubscriptionDispatchResult:
        sessions = deps.storage_provider.get_storage(WorkspaceSession)
        session = await sessions.get(sub.config.session_id)

        # Session must still exist + still be parked on the same
        # tool_call_id. Anything else (ended, resumed by another path,
        # park moved on to a different tool) means the subscription has
        # been orbitally orphaned — drop it and report a structured skip.
        if session is None or session.status == SessionStatus.ENDED:
            await _delete_sub(deps, sub.id)
            return SubscriptionDispatchResult(
                ok=True,
                skipped=True,
                error_code="skipped_session_unparked",
                error_message=(
                    "session no longer exists"
                    if session is None
                    else "session ended"
                ),
            )
        if session.parked_status != "parked":
            await _delete_sub(deps, sub.id)
            return SubscriptionDispatchResult(
                ok=True,
                skipped=True,
                error_code="skipped_session_unparked",
                error_message="session no longer parked",
            )
        parked_state = session.parked_state or {}
        if parked_state.get("tool_call_id") != sub.config.tool_call_id:
            await _delete_sub(deps, sub.id)
            return SubscriptionDispatchResult(
                ok=True,
                skipped=True,
                error_code="skipped_session_unparked",
                error_message=(
                    "session parked on a different tool_call_id"
                ),
            )

        # Build the tool result envelope. The rendered payload, when
        # parseable as JSON, becomes the ``payload`` field; otherwise
        # the raw string is passed through so the agent can read it as
        # text. ``fire_context`` always surfaces alongside so the
        # agent's resume hook can inspect which trigger / when fired.
        payload: object
        if rendered_payload:
            try:
                payload = json.loads(rendered_payload)
            except (TypeError, json.JSONDecodeError):
                payload = rendered_payload
        else:
            payload = fire_context
        tool_result = {
            "ok": True,
            "fire_context": fire_context,
            "payload": payload,
        }

        try:
            await respond_to_yield(
                session_id=sub.config.session_id,
                tool_call_id=sub.config.tool_call_id,
                result=tool_result,
                deps=RespondToYieldDeps(
                    storage_provider=deps.storage_provider,
                    event_bus=deps.event_bus,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — defensive perimeter
            return SubscriptionDispatchResult(
                ok=False,
                error_code="dispatch_failed",
                error_message=str(exc),
            )
        await _delete_sub(deps, sub.id)
        return SubscriptionDispatchResult(
            ok=True, artefact_id=sub.config.session_id,
        )


async def _delete_sub(deps: DispatchDeps, sub_id: str) -> None:
    """Best-effort delete of the consumed ``parked_session`` subscription.

    A leaked row would let a later scheduled fire try to wake a session
    that's already moved on; a delete failure is logged but never
    propagated — the dispatch itself succeeded and the orphan row will
    naturally fail-skip on its next attempt.
    """
    storage = deps.storage_provider.get_storage(Subscription)
    try:
        await storage.delete(sub_id)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        logger.warning(
            "parked_session dispatcher: failed to delete consumed "
            "subscription %r: %s",
            sub_id, exc,
        )


register("parked_session", ParkedSessionDispatcher())


__all__ = ["ParkedSessionDispatcher"]
