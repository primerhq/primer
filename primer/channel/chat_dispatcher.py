"""Chat -> channel relay + gate forwarding (analogue of ChannelDispatcher).

Out-of-process safety
---------------------
The inbound channel gateway (Telegram long-poll, Slack socket, Discord
gateway) must live in exactly ONE process — the one that owns inbound
(the API). A worker that runs chat turns out-of-process must NOT open a
second inbound connection just to post an outbound reply (that triggers
Telegram 409 Conflict / duplicate Slack+Discord deliveries).

So relay never *builds* an adapter on the worker: it asks the registry
for an already-warm one (:meth:`ChannelRegistry.peek_adapter`). When the
worker is in-process with the API (``api+worker``) the warm adapter is
there and we post directly. When the worker is separate the peek misses
and we publish a tiny ``chat:<id>:relay`` signal on the event bus; the
API process re-derives the payload from storage (the source of truth)
and posts via its own warm adapter. The bus payload carries only a
``kind`` discriminator — never the text/envelope — to stay well under
the 8000-byte ``pg_notify`` limit.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from primer.channel.adapter import PromptEnvelope
from primer.int.storage_provider import StorageProvider
from primer.model.channel import ChatChannelAssociation
from primer.model.chats import Chat, ChatMessage
from primer.model.storage import OffsetPage, OrderBy
from primer.storage.q import Q


if TYPE_CHECKING:
    from primer.api.registries.channel_registry import ChannelRegistry
    from primer.int.event_bus import EventBus


logger = logging.getLogger(__name__)


def relay_event_key(chat_id: str) -> str:
    """Event-bus key a worker publishes to ask the inbound-owning process
    to relay this chat's pending output/gate to its channel."""
    return f"chat:{chat_id}:relay"


def parse_relay_event_key(event_key: str) -> str | None:
    """Inverse of :func:`relay_event_key`: the chat id, or None if the key
    is not a chat-relay key."""
    if event_key.startswith("chat:") and event_key.endswith(":relay"):
        cid = event_key[len("chat:"):-len(":relay")]
        return cid or None
    return None


async def derive_final_relay_text(
    storage_provider: StorageProvider, chat_id: str,
) -> str | None:
    """Re-derive the text relay_mode 'final' posts: the joined assistant
    deltas of the LAST completed turn (between the previous and final
    ``done`` rows). Returns None when there is no completed turn / no text.

    Storage is the source of truth, so this is identical whether called
    inline on the worker (in-proc) or by the API after a bus signal.
    """
    msgs = storage_provider.get_storage(ChatMessage)
    rows: list[ChatMessage] = []
    offset = 0
    while True:
        page = await msgs.find(
            Q(ChatMessage).where("chat_id", chat_id).build(),
            OffsetPage(offset=offset, length=200),
            order_by=[OrderBy(field="seq", direction="asc")],
        )
        rows.extend(page.items)
        if len(page.items) < 200:
            break
        offset += 200
    last_done = max(
        (i for i, r in enumerate(rows) if r.kind == "done"), default=None)
    if last_done is None:
        return None
    prev_done = max(
        (i for i in range(last_done) if rows[i].kind == "done"), default=-1)
    chunks: list[str] = []
    for r in rows[prev_done + 1:last_done]:
        if r.kind == "assistant_token":
            delta = (r.payload or {}).get("delta")
            if isinstance(delta, str):
                chunks.append(delta)
    text = "".join(chunks).strip()
    return text or None


async def derive_chat_gate_envelope(
    storage_provider: StorageProvider, chat_id: str,
) -> PromptEnvelope | None:
    """Re-derive the gate envelope for a chat's current pending tool call.

    Mirrors what the worker builds inline when forwarding a freshly-set
    gate; reused by the API-side relay forwarder so the envelope is rebuilt
    from the persisted ``pending_tool_call`` rather than shipped over the
    bus. Returns None when no gate is pending.
    """
    chat = await storage_provider.get_storage(Chat).get(chat_id)
    if chat is None or chat.pending_tool_call is None:
        return None
    pending = chat.pending_tool_call
    mode = pending.get("mode")
    kind = "tool_approval" if mode == "approval" else "ask_user"
    if mode == "approval":
        original = pending.get("original_call") or {}
        prompt = f"Approve running `{original.get('name', '?')}`?"
    else:
        prompt = "The agent is asking for your input."
    return PromptEnvelope(
        kind=kind, workspace_id="", session_id=chat_id,
        tool_call_id=pending.get("tool_call_id", ""), prompt=prompt,
        response_schema=pending.get("response_schema"), choices=None,
        timeout_at_iso=None)


class ChatChannelDispatcher:
    """Relays a bound chat's output + gates to its channel adapter.

    Two roles, selected by ``allow_build``:

    * Worker (default, ``allow_build=False``): never builds an adapter.
      Posts via a warm adapter when one is cached locally (in-proc
      ``api+worker``); otherwise publishes a bus relay signal for the
      inbound-owning process to fulfil. Requires ``event_bus`` for the
      latter; without it a cold relay is dropped (logged).
    * Relay forwarder (``allow_build=True``): the inbound-owning process,
      which may build/warm the adapter. Never republishes (no bus loop).
    """

    def __init__(
        self,
        *,
        storage_provider: StorageProvider,
        registry: "ChannelRegistry",
        event_bus: "EventBus | None" = None,
        allow_build: bool = False,
    ) -> None:
        self._sp = storage_provider
        self._registry = registry
        self._bus = event_bus
        self._allow_build = allow_build

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

    async def _adapter_for(self, channel_id: str):
        """Resolve the adapter to post through, honouring the build policy.

        ``allow_build`` callers may build+warm (they own inbound); the
        default worker path is cache-only so it cannot open a second
        inbound gateway."""
        if self._allow_build:
            return await self._registry.get_adapter(channel_id)
        return self._registry.peek_adapter(channel_id)

    async def _publish_relay(self, chat_id: str, kind: str) -> bool:
        """No warm adapter here: ask the inbound-owning process to relay."""
        if self._bus is None:
            logger.warning(
                "chat relay for %s: no warm adapter and no event bus; "
                "dropping (%s)", chat_id, kind,
            )
            return False
        await self._bus.publish(relay_event_key(chat_id), {"kind": kind})
        return True

    async def relay_text(self, *, chat_id: str, text: str) -> bool:
        resolved = await self._resolve(chat_id)
        if resolved is None:
            return False
        chat, assoc = resolved
        if not assoc.forward_inform:
            return False
        adapter = await self._adapter_for(chat.channel_binding.channel_id)
        if adapter is None:
            return await self._publish_relay(chat_id, "text")
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
        adapter = await self._adapter_for(chat.channel_binding.channel_id)
        if adapter is None:
            return await self._publish_relay(chat_id, "gate")
        try:
            await adapter.post_prompt(envelope)
            return True
        except Exception as exc:
            logger.warning("chat gate post failed for %s: %s", chat_id, exc)
            return False


__all__ = [
    "ChatChannelDispatcher",
    "derive_chat_gate_envelope",
    "derive_final_relay_text",
    "parse_relay_event_key",
    "relay_event_key",
]
