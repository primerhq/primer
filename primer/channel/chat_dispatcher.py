"""Chat -> channel relay + gate forwarding (analogue of ChannelDispatcher)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from primer.channel.adapter import PromptEnvelope
from primer.int.storage_provider import StorageProvider
from primer.model.channel import ChatChannelAssociation
from primer.model.chats import Chat
from primer.model.storage import OffsetPage
from primer.storage.q import Q


if TYPE_CHECKING:
    from primer.api.registries.channel_registry import ChannelRegistry


logger = logging.getLogger(__name__)


class ChatChannelDispatcher:
    """Relays a bound chat's output + gates to its channel adapter."""

    def __init__(
        self, *, storage_provider: StorageProvider, registry: "ChannelRegistry",
    ) -> None:
        self._sp = storage_provider
        self._registry = registry

    async def _resolve(
        self, chat_id: str,
    ) -> tuple[Chat, ChatChannelAssociation] | None:
        chat = await self._sp.get_storage(Chat).get(chat_id)
        if chat is None or chat.channel_binding is None:
            return None
        page = await self._sp.get_storage(ChatChannelAssociation).find(
            Q(ChatChannelAssociation)
            .where("channel_id", chat.channel_binding.channel_id).build(),
            OffsetPage(offset=0, length=1),
        )
        if not page.items or not page.items[0].enabled:
            return None
        return chat, page.items[0]

    async def relay_text(self, *, chat_id: str, text: str) -> bool:
        resolved = await self._resolve(chat_id)
        if resolved is None:
            return False
        chat, assoc = resolved
        if not assoc.forward_inform:
            return False
        adapter = await self._registry.get_adapter(
            chat.channel_binding.channel_id)
        try:
            post_chat = getattr(adapter, "post_chat_message", None)
            if post_chat is not None:
                thread_ts = chat.channel_binding.thread_external_id
                try:
                    if thread_ts is not None:
                        await post_chat(text, thread_ts=thread_ts)
                    else:
                        await post_chat(text)
                except TypeError:
                    await post_chat(text)
            else:
                env = PromptEnvelope(
                    kind="inform", workspace_id="",
                    session_id=chat.channel_binding.thread_external_id or "",
                    tool_call_id="", prompt=text, response_schema=None,
                    choices=None, timeout_at_iso=None)
                await adapter.post_prompt(env)
            return True
        except Exception as exc:
            logger.warning("chat relay post failed for %s: %s", chat_id, exc)
            return False

    async def dispatch_gate(
        self, *, chat_id: str, envelope: PromptEnvelope,
    ) -> bool:
        resolved = await self._resolve(chat_id)
        if resolved is None:
            return False
        chat, assoc = resolved
        flag = {
            "ask_user": assoc.forward_ask_user,
            "tool_approval": assoc.forward_tool_approval,
            "inform": assoc.forward_inform,
        }.get(envelope.kind, assoc.forward_tool_approval)
        if not flag:
            return False
        adapter = await self._registry.get_adapter(
            chat.channel_binding.channel_id)
        try:
            await adapter.post_prompt(envelope)
            return True
        except Exception as exc:
            logger.warning("chat gate post failed for %s: %s", chat_id, exc)
            return False


__all__ = ["ChatChannelDispatcher"]
