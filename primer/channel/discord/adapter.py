"""DiscordChannelAdapter — one per Channel row."""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.adapter import (
    ChannelAdapter, PromptEnvelope, ResponseEnvelope,
    format_tool_args, session_thread_label,
)
from primer.channel.discord.connection import DISCORD_CONNECTIONS
from primer.channel.discord.views import ApprovalView
from primer.model.channel import Channel, ChannelProvider
from primer.model.except_ import ProviderError


logger = logging.getLogger(__name__)

# Discord message content caps at 2000 chars; keep args well under it.
_DISCORD_ARGS_MAX = 1700


def format_approval_content(envelope: PromptEnvelope) -> str:
    """Render a tool-approval prompt as Discord markdown: tool name plus a
    pretty-printed JSON code block, instead of the raw ``prompt`` string.
    """
    tool_name = envelope.tool_name or "(unknown tool)"
    args_json = format_tool_args(envelope.tool_args)
    if len(args_json) > _DISCORD_ARGS_MAX:
        args_json = args_json[:_DISCORD_ARGS_MAX] + "\n... (truncated)"
    return (
        ":lock: **Tool approval requested**\n"
        f"**Tool:** `{tool_name}`\n"
        f"**Arguments:**\n```json\n{args_json}\n```"
    )


class DiscordChannelAdapter(ChannelAdapter):
    """Per-channel Discord adapter."""

    def __init__(
        self, *, provider: ChannelProvider, channel: Channel, inbox,
        storage_provider=None, event_bus=None, claim_engine=None,
        artifact_registry=None,
    ) -> None:
        self._provider = provider
        self._channel = channel
        self._inbox = inbox
        # Chat-surface wiring. Optional so existing callers (session/workspace
        # channels) keep working; the chat dispatch path stays inactive when
        # _sp is None.
        self._sp = storage_provider
        self._bus = event_bus
        self._claim_engine = claim_engine
        self._artifacts = artifact_registry
        self._client: Any | None = None
        # session_id → discord Thread id (one conversation thread per session)
        self._session_threads: dict[str, int] = {}
        # thread_id (int) → {workspace_id, session_id, tool_call_id} for the
        # ask_user currently awaiting a reply in that thread (sessions park on
        # one prompt at a time).
        self._pending_ask: dict[int, dict[str, str]] = {}

    async def initialize(self) -> None:
        self._client = await DISCORD_CONNECTIONS.acquire(self._provider)
        entry = DISCORD_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_channel_id[str(self._channel.external_id)] = self

    async def aclose(self) -> None:
        entry = DISCORD_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_channel_id.pop(str(self._channel.external_id), None)
        if self._client is not None:
            await DISCORD_CONNECTIONS.release(self._provider)
            self._client = None

    async def verify(self) -> None:
        if self._client is None:
            raise ProviderError("DiscordChannelAdapter used before initialize()")
        me = self._client.user
        if me is None or getattr(me, "id", 0) == 0:
            raise ProviderError("discord gateway login failed (no bot user)")
        try:
            channel = self._client.get_channel(int(self._channel.external_id))
            if channel is None:
                channel = await self._client.fetch_channel(
                    int(self._channel.external_id),
                )
        except Exception as exc:
            raise ProviderError(
                f"discord channel {self._channel.external_id!r} not reachable: {exc}"
            ) from exc
        if channel is None:
            raise ProviderError(
                f"discord channel {self._channel.external_id!r} not reachable"
            )

    async def post_prompt(self, envelope: PromptEnvelope) -> dict[str, Any]:
        if self._client is None:
            raise ProviderError("DiscordChannelAdapter used before initialize()")
        channel = self._client.get_channel(int(self._channel.external_id))
        if channel is None:
            channel = await self._client.fetch_channel(
                int(self._channel.external_id),
            )
        if channel is None:
            raise ProviderError(
                f"discord channel {self._channel.external_id!r} not reachable"
            )
        thread = await self._session_thread(channel, envelope.session_id)
        if envelope.kind == "tool_approval":
            view = ApprovalView(
                ws=envelope.workspace_id,
                sid=envelope.session_id,
                tcid=envelope.tool_call_id,
            )
            msg = await thread.send(
                content=format_approval_content(envelope), view=view,
            )
            return {"message_id": getattr(msg, "id", 0), "thread_id": thread.id}
        elif envelope.kind == "ask_user":
            msg = await thread.send(content=envelope.prompt)
            self._pending_ask[thread.id] = {
                "workspace_id": envelope.workspace_id,
                "session_id": envelope.session_id,
                "tool_call_id": envelope.tool_call_id,
            }
            return {"message_id": getattr(msg, "id", 0), "thread_id": thread.id}
        elif envelope.kind == "inform":
            msg = await thread.send(content=envelope.prompt)
            return {"message_id": getattr(msg, "id", 0), "thread_id": thread.id}
        else:
            raise ProviderError(f"unknown envelope kind {envelope.kind!r}")

    async def _session_thread(self, channel: Any, session_id: str) -> Any:
        """Get-or-create the one conversation thread for this session.

        The first prompt posts a small anchor message and opens a named thread
        off it; every later prompt for the same session (ask or approval) is
        sent into that thread.
        """
        tid = self._session_threads.get(session_id)
        if tid is not None:
            thread = self._client.get_channel(tid)
            if thread is None:
                try:
                    thread = await self._client.fetch_channel(tid)
                except Exception:
                    thread = None
            if thread is not None:
                return thread
        label = session_thread_label(session_id)
        anchor = await channel.send(content=f":thread: {label}")
        thread = await anchor.create_thread(
            name=label[:100], auto_archive_duration=60,
        )
        self._session_threads[session_id] = thread.id
        return thread

    async def _handle_decision(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        decision: str, reason: str | None,
        discord_user_id: int | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="tool_approval",
            workspace_id=workspace_id, session_id=session_id,
            tool_call_id=tool_call_id,
            response=None, decision=decision, reason=reason,
            platform_metadata={"discord_user_id": discord_user_id or 0},
        ))

    async def _handle_text_reply(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        text: str,
        discord_user_id: int | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="ask_user",
            workspace_id=workspace_id, session_id=session_id,
            tool_call_id=tool_call_id,
            response=text, decision=None, reason=None,
            platform_metadata={"discord_user_id": discord_user_id or 0},
        ))

    async def _chat_thread_name(self, thread_ts: str) -> str:
        """Build a friendly thread name: "{agent label}: {first words}".

        Resolves the Chat bound to (this channel, thread_ts), its agent's
        ``description`` (falling back to the agent id), and the first ~6 words
        of the chat's first user_message. Returns "chat" if anything is
        missing; the caller wraps this in try/except for a hard fallback.
        """
        from primer.model.agent import Agent
        from primer.model.chats import Chat, ChatMessage
        from primer.model.storage import OffsetPage, OrderBy
        from primer.storage.q import Q

        chats = self._sp.get_storage(Chat)
        chat = None
        offset = 0
        while True:
            page = await chats.find(None, OffsetPage(offset=offset, length=200))
            for c in page.items:
                b = c.channel_binding
                if (
                    b is not None
                    and b.channel_id == self._channel.id
                    and b.thread_external_id == thread_ts
                ):
                    chat = c
                    break
            if chat is not None or len(page.items) < 200:
                break
            offset += 200
        if chat is None:
            return "chat"
        agent = await self._sp.get_storage(Agent).get(chat.agent_id)
        label = (agent.description if agent is not None else None) or chat.agent_id
        # First user_message's content -> first ~6 words.
        snippet = ""
        msgs = self._sp.get_storage(ChatMessage)
        mpage = await msgs.find(
            Q(ChatMessage).where("chat_id", chat.id).build(),
            OffsetPage(offset=0, length=50),
            order_by=[OrderBy(field="seq", direction="asc")],
        )
        for m in mpage.items:
            if m.kind == "user_message":
                content = (m.payload or {}).get("content") or ""
                content = content.strip()
                # Strip a leading "[sender] " attribution prefix if present.
                if content.startswith("[") and "] " in content:
                    content = content.split("] ", 1)[1]
                snippet = " ".join(content.split()[:6])
                break
        name = f"{label}: {snippet}" if snippet else str(label)
        return name[:100] or "chat"

    async def _resolve_chat_thread(self, thread_ts: str | None) -> Any:
        """Resolve (or create) the discord.py thread for a chat's anchor id.

        ``thread_ts`` is the string id stored in
        ``ChatChannelBinding.thread_external_id``: for a top-level message it is
        the anchor MESSAGE id (no thread exists yet); for an in-thread reply it
        is the thread id. We first look up an existing thread/channel with that
        id; if none exists we treat ``thread_ts`` as the anchor message and open
        a thread off it (Discord gives the thread the anchor message's id, so it
        matches both this binding and inbound thread replies). ``None`` -> the
        parent channel.
        """
        if self._client is None:
            raise ProviderError("DiscordChannelAdapter used before initialize()")
        parent_id = int(self._channel.external_id)
        channel = self._client.get_channel(parent_id)
        if channel is None:
            channel = await self._client.fetch_channel(parent_id)
        if channel is None:
            raise ProviderError(
                f"discord channel {self._channel.external_id!r} not reachable"
            )
        if thread_ts is None:
            return channel
        tid = int(thread_ts)
        # Existing thread (created on a prior relay, or the in-thread case)?
        thread = self._client.get_channel(tid)
        if thread is None:
            try:
                thread = await self._client.fetch_channel(tid)
            except Exception:
                thread = None
        if thread is not None:
            return thread
        # No thread yet: open one off the anchor message so chat replies stay
        # in a per-chat thread instead of the parent channel.
        try:
            anchor = await channel.fetch_message(tid)
        except Exception:
            return channel  # anchor gone / unreachable: degrade to the channel
        try:
            name = await self._chat_thread_name(thread_ts)
        except Exception:
            name = f"chat {tid}"
        try:
            return await anchor.create_thread(
                name=name[:100], auto_archive_duration=60,
            )
        except Exception:
            # Thread may already exist (race): resolve it once more, else channel.
            thread = self._client.get_channel(tid)
            if thread is None:
                try:
                    thread = await self._client.fetch_channel(tid)
                except Exception:
                    thread = None
            return thread if thread is not None else channel

    async def post_chat_message(
        self, text: str, *, thread_ts: str | None = None
    ) -> dict[str, Any]:
        """Full-payload outbound relay into the chat's thread."""
        target = await self._resolve_chat_thread(thread_ts)
        await target.send(content=text)
        return {"thread_id": thread_ts}

    async def _build_media_parts(
        self, attachments: list, text: str,
    ) -> tuple[list, str]:
        """Turn discord Attachments into persisted, artifact-backed chat Parts.

        Returns ``(parts, text)`` where ``text`` may gain a
        " [attachment skipped: ...]" note for any attachment the media layer
        rejects (too large / disallowed type). Media is skipped entirely (parts
        empty, text unchanged) when no artifact store is wired or chat is off
        (``self._artifacts is None`` / ``self._sp is None``)."""
        from primer.channel.media import MediaError, store_inbound_media

        if not attachments or self._artifacts is None or self._sp is None:
            return [], text
        try:
            store = await self._artifacts.get_default()
        except Exception:
            logger.exception("discord: artifact store unavailable; skipping media")
            return [], text
        parts: list = []
        for att in attachments:
            try:
                data = await att.read()
                part = await store_inbound_media(
                    store, data=data,
                    mime_type=getattr(att, "content_type", None),
                    filename=getattr(att, "filename", None),
                )
            except MediaError:
                text = (text or "") + " [attachment skipped: too large]"
                continue
            except Exception:
                logger.exception("discord: failed to ingest attachment; skipping")
                text = (text or "") + " [attachment skipped]"
                continue
            parts.append(part)
        return parts, text

    async def handle_inbound_chat_message(
        self, *, thread_id: str | None, message_id: str,
        sender_name: str, text: str, attachments: list | None = None,
    ):
        """Multi-type inbound: a top-level message opens a new thread-chat; an
        in-thread message routes to that thread's chat. The thread id is the
        message id on a top-level message (the new thread anchors on it).

        ``attachments`` is the raw ``discord.Message.attachments`` list (each
        with ``.read()``, ``.content_type``, ``.filename``); each is stored as
        an artifact-backed media Part and routed alongside the caption text."""
        from primer.channel.chat_inbox import ChatResponseInbox
        from primer.channel.chat_router import ChatChannelRouter
        thread_external_id = thread_id or message_id
        media_parts, text = await self._build_media_parts(
            attachments or [], text)
        gate_inbox = ChatResponseInbox(
            storage_provider=self._sp, event_bus=self._bus,
            claim_engine=self._claim_engine)
        router = ChatChannelRouter(
            storage_provider=self._sp, event_bus=self._bus, gate_inbox=gate_inbox,
            claim_engine=self._claim_engine)
        chat, _ = await router.deliver_message(
            channel_id=self._channel.id, thread_external_id=thread_external_id,
            supports_threads=True, sender_name=sender_name, text=text,
            media_parts=media_parts or None)
        return chat


__all__ = ["DiscordChannelAdapter"]
