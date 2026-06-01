"""chat_message subscription dispatcher — Spec §5.1.

A ``chat_message`` subscription points at an existing :class:`Chat`
row. On fire, the dispatcher appends one ``user_message`` to the
chat (carrying the rendered payload as a single text part), flips
``turn_status`` to ``"claimable"``, and pulses the claim engine so a
worker picks the chat up. Mirrors what
:mod:`primer.api.routers.chats` does when a human sends a WS frame —
the canonical persist path lives in :mod:`primer.chat.enqueue`.

Parallelism: ``skip`` returns a no-op skip when the chat is already
mid-turn (``turn_status == "running"``); ``queue`` always appends and
relies on the chat's FIFO message log to deliver the turn after the
in-flight one drains.
"""

from __future__ import annotations

import logging

from primer.chat.enqueue import append_user_message
from primer.model.chats import Chat
from primer.model.trigger import Subscription
from primer.trigger.subscribers import (
    DispatchDeps,
    SubscriptionDispatchResult,
    register,
)


logger = logging.getLogger(__name__)


class ChatMessageDispatcher:
    """Dispatcher for ``chat_message`` subscriptions."""

    kind = "chat_message"

    async def dispatch(
        self,
        sub: Subscription,
        *,
        rendered_payload: str,
        fire_context: dict,
        fire_id: str,
        deps: DispatchDeps,
    ) -> SubscriptionDispatchResult:
        chats_storage = deps.storage_provider.get_storage(Chat)
        chat = await chats_storage.get(sub.config.chat_id)
        if chat is None:
            return SubscriptionDispatchResult(
                ok=False,
                error_code="chat_not_found",
                error_message=f"chat {sub.config.chat_id!r} not found",
            )
        if chat.status == "ended":
            return SubscriptionDispatchResult(
                ok=False,
                error_code="chat_ended",
                error_message=f"chat {sub.config.chat_id!r} is ended",
            )
        if sub.parallelism == "skip" and chat.turn_status == "running":
            return SubscriptionDispatchResult(
                ok=True,
                skipped=True,
                error_code="skipped_chat_busy",
                error_message="chat already running a turn",
            )

        msg = await append_user_message(
            chat=chat,
            parts=[{"type": "text", "text": rendered_payload}],
            storage_provider=deps.storage_provider,
            attribution={
                "trigger_id": sub.trigger_id,
                "subscription_id": sub.id,
                "fire_id": fire_id,
            },
        )
        # Flip turn_status to claimable + upsert claim so worker picks
        # the chat up on the next claim-loop pass. Claim-engine pulse
        # is a best-effort hint; the periodic poll will pick the chat
        # up even if the upsert call fails.
        chat.turn_status = "claimable"
        await chats_storage.update(chat)
        if deps.claim_engine is not None:
            try:
                from primer.int.claim import ClaimKind

                await deps.claim_engine.upsert(
                    ClaimKind.CHAT, chat.id, priority=10,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort pulse
                logger.warning(
                    "chat_message dispatcher: claim_engine.upsert(%r) "
                    "raised: %s",
                    chat.id, exc,
                )
        return SubscriptionDispatchResult(ok=True, artefact_id=msg.id)


register("chat_message", ChatMessageDispatcher())


__all__ = ["ChatMessageDispatcher"]
