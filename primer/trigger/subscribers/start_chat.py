"""start_chat subscription dispatcher — Spec §6.4, §14 decision 2.

``start_chat`` is the one genuinely new action: it creates a fresh
:class:`Chat` bound to the source thread, seeds the firing event's
rendered text as the first ``user_message``, flips ``turn_status`` to
``"claimable"``, and pulses the claim engine so a worker runs it. It
mirrors :meth:`primer.channel.chat_router.ChatChannelRouter._new_chat`
plus the ``chat_message`` dispatcher's seed-and-pulse path.

The firing :class:`ChannelEvent` is reconstructed from
``fire_context["event"]``; its ``channel_id`` + ``thread_anchor`` become
the new chat's :class:`ChatChannelBinding` so outbound relay and
inbound thread->chat correlation line up.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from primer.chat.enqueue import append_user_message
from primer.model.agent import Agent
from primer.model.channel_event import ChannelEvent
from primer.model.chats import Chat, ChatChannelBinding
from primer.model.trigger import Subscription
from primer.trigger.subscribers import (
    DispatchDeps,
    SubscriptionDispatchResult,
    register,
)


logger = logging.getLogger(__name__)


class StartChatDispatcher:
    """Dispatcher for ``start_chat`` subscriptions."""

    kind = "start_chat"

    async def dispatch(
        self,
        sub: Subscription,
        *,
        rendered_payload: str,
        fire_context: dict,
        fire_id: str,
        deps: DispatchDeps,
    ) -> SubscriptionDispatchResult:
        raw_event = fire_context.get("event")
        if raw_event is None:
            return SubscriptionDispatchResult(
                ok=False,
                error_code="no_event",
                error_message="start_chat fired with no channel event in context",
            )
        event = ChannelEvent.model_validate(raw_event)

        agent = await deps.storage_provider.get_storage(Agent).get(
            sub.config.agent_id,
        )
        if agent is None:
            return SubscriptionDispatchResult(
                ok=False,
                error_code="agent_not_found",
                error_message=f"Agent {sub.config.agent_id!r} does not exist",
            )

        chats_storage = deps.storage_provider.get_storage(Chat)
        binding = None
        if event.channel_id:
            binding = ChatChannelBinding(
                channel_id=event.channel_id,
                thread_external_id=event.thread_anchor,
            )
        chat = Chat(
            id=f"chat-{uuid.uuid4().hex[:12]}",
            agent_id=sub.config.agent_id,
            created_at=datetime.now(timezone.utc),
            channel_binding=binding,
        )
        await chats_storage.create(chat)

        await append_user_message(
            chat=chat,
            parts=[{"type": "text", "text": rendered_payload}],
            storage_provider=deps.storage_provider,
            attribution={
                "trigger_id": sub.trigger_id,
                "subscription_id": sub.id,
                "fire_id": fire_id,
            },
        )

        chat = await chats_storage.get(chat.id)
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
                    "start_chat dispatcher: claim_engine.upsert(%r) "
                    "raised: %s",
                    chat.id, exc,
                )

        return SubscriptionDispatchResult(ok=True, artefact_id=chat.id)


register("start_chat", StartChatDispatcher())


__all__ = ["StartChatDispatcher"]
