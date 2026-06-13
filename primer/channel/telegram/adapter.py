"""TelegramChannelAdapter — one per Channel row."""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

from primer.channel.adapter import (
    ChannelAdapter, PromptEnvelope, ResponseEnvelope, attribution_header,
)
from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS
from primer.channel.telegram.render import (
    build_ask_user_message,
    build_tool_approval_message,
    compute_tag,
)
from primer.model.channel import Channel, ChannelProvider
from primer.model.chats import Chat
from primer.model.except_ import ProviderError


logger = logging.getLogger(__name__)

# Max correlation entries kept per adapter. Sized for a busy bot's recent
# in-flight prompts; older entries are evicted (their parks, if still open,
# fall back to the storage row on resume).
_CACHE_MAXSIZE = 10_000

# Agents shown per page in the /agent inline-keyboard picker.
_AGENTS_PER_PAGE = 8


class _BoundedDict(OrderedDict):
    """An insertion-ordered dict that evicts the oldest entry once it
    exceeds ``maxsize``. Re-inserting an existing key refreshes its
    recency (move-to-end)."""

    def __init__(self, *, maxsize: int) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            self.popitem(last=False)


class TelegramChannelAdapter(ChannelAdapter):
    """Per-channel Telegram adapter."""

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
        # Inbound media limits/compression tunables. Tests may override.
        self._media_config = None
        self._app: Any | None = None
        # tag -> ids, for the Approve/Reject button callbacks. Bounded so a
        # long-lived bot does not grow these caches without limit (one entry
        # per prompt sent); the oldest correlations fall off first.
        self._tag_cache: _BoundedDict = _BoundedDict(maxsize=_CACHE_MAXSIZE)
        # message_id -> {**ids, "kind": "ask_user" | "reject"}, so a text
        # reply is correlated by the message it replies to (no visible
        # token in the message body).
        self._reply_targets: _BoundedDict = _BoundedDict(maxsize=_CACHE_MAXSIZE)

    def remember_reply_target(
        self, *, message_id: int, ids: dict[str, str], kind: str,
    ) -> None:
        self._reply_targets[message_id] = {**ids, "kind": kind}

    def resolve_reply_target(self, message_id: int) -> dict[str, str] | None:
        return self._reply_targets.get(message_id)

    async def initialize(self) -> None:
        self._app = await TELEGRAM_CONNECTIONS.acquire(self._provider)
        entry = TELEGRAM_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_chat_id[str(self._channel.external_id)] = self

    async def aclose(self) -> None:
        entry = TELEGRAM_CONNECTIONS.entry(self._provider.id)
        if entry is not None:
            entry.adapters_by_chat_id.pop(str(self._channel.external_id), None)
        if self._app is not None:
            await TELEGRAM_CONNECTIONS.release(self._provider)
            self._app = None

    async def verify(self) -> None:
        if self._app is None:
            raise ProviderError("TelegramChannelAdapter used before initialize()")
        me = await self._app.bot.get_me()
        if not me.username:
            raise ProviderError("telegram getMe returned no username")
        try:
            chat = await self._app.bot.get_chat(
                chat_id=int(self._channel.external_id),
            )
        except Exception as exc:
            raise ProviderError(
                f"telegram chat {self._channel.external_id!r} not reachable: {exc}"
            ) from exc
        if chat is None:
            raise ProviderError(
                f"telegram chat {self._channel.external_id!r} not reachable"
            )

    async def post_prompt(self, envelope: PromptEnvelope) -> dict[str, Any]:
        if self._app is None:
            raise ProviderError("TelegramChannelAdapter used before initialize()")
        await self._post_envelope_media(envelope)
        header = attribution_header(envelope)
        if envelope.kind == "inform":
            msg = await self._app.bot.send_message(
                chat_id=self._channel.external_id,
                text=header + envelope.prompt,
            )
            return {"message_id": getattr(msg, "message_id", 0)}
        if envelope.kind == "ask_user":
            body = build_ask_user_message(
                chat_id=self._channel.external_id, envelope=envelope,
            )
        elif envelope.kind == "tool_approval":
            body = build_tool_approval_message(
                chat_id=self._channel.external_id, envelope=envelope,
            )
        else:
            raise ProviderError(f"unknown envelope kind {envelope.kind!r}")
        if header:
            body["text"] = header + body["text"]
        tag = compute_tag(
            workspace_id=envelope.workspace_id,
            session_id=envelope.session_id,
            tool_call_id=envelope.tool_call_id,
        )
        ids = {
            "workspace_id": envelope.workspace_id,
            "session_id": envelope.session_id,
            "tool_call_id": envelope.tool_call_id,
        }
        self._tag_cache[tag] = ids
        msg = await self._app.bot.send_message(**body)
        message_id = getattr(msg, "message_id", 0)
        # ask_user is answered by a text reply -> correlate by message id.
        # tool_approval is answered by the inline buttons (callback_data).
        if envelope.kind == "ask_user" and message_id:
            self.remember_reply_target(
                message_id=message_id, ids=ids, kind="ask_user",
            )
        return {"message_id": message_id}

    async def _resolve_tag(self, tag: str) -> dict[str, str] | None:
        cached = self._tag_cache.get(tag)
        if cached is not None:
            return cached
        # Cold-lookup fallback. Inject the session storage via the
        # registry pattern (test will populate cache, so cold path
        # only runs in real deployments after restart).
        return None

    async def _handle_decision(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        decision: str, reason: str | None,
        telegram_user_id: int | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="tool_approval",
            workspace_id=workspace_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            response=None, decision=decision, reason=reason,
            platform_metadata={
                "telegram_user_id": telegram_user_id or 0,
            },
        ))

    async def _handle_text_reply(
        self, *,
        workspace_id: str, session_id: str, tool_call_id: str,
        text: str,
        telegram_user_id: int | None,
    ) -> None:
        await self._inbox.handle_response(ResponseEnvelope(
            kind="ask_user",
            workspace_id=workspace_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            response=text,
            decision=None, reason=None,
            platform_metadata={
                "telegram_user_id": telegram_user_id or 0,
            },
        ))

    async def handle_inbound_chat_text(
        self, *, sender_name: str, text: str,
    ) -> str | None:
        """Dispatch a single-type inbound message: command or chat turn.
        Returns a notice string to post back (commands) or None (routed to a
        turn)."""
        if self._sp is None:
            return None
        from primer.channel.chat_inbox import ChatResponseInbox
        from primer.channel.chat_router import ChatChannelRouter
        from primer.channel.commands import (
            CommandExecutor, help_text, parse_command,
        )
        parsed = parse_command(text)
        if parsed is not None:
            ex = CommandExecutor(storage_provider=self._sp)
            if parsed.verb == "help":
                return help_text(supports_threads=False)
            if parsed.verb == "new":
                res = await ex.new_single_chat(channel_id=self._channel.id)
                return res.text
            if parsed.verb == "list":
                res = await ex.list_chats(channel_id=self._channel.id)
                if not res.items:
                    return "No chats yet."
                return "\n".join(
                    f"- {it['title']} ({it['agent_id']}) {it['chat_id']}"
                    for it in res.items)
            if parsed.verb == "switch" and parsed.arg:
                res = await ex.switch_active_chat(
                    channel_id=self._channel.id, chat_id=parsed.arg)
                return res.text
            if parsed.verb == "agent" and parsed.arg:
                router = ChatChannelRouter(storage_provider=self._sp)
                chat, _ = await router.resolve_or_create(
                    channel_id=self._channel.id, thread_external_id=None,
                    supports_threads=False)
                res = await ex.set_agent(chat_id=chat.id, agent_id=parsed.arg)
                return res.text
            if parsed.verb == "agent":
                from primer.channel.chat_router import ChatChannelRouter as _R
                router = _R(storage_provider=self._sp)
                chat, _ = await router.resolve_or_create(
                    channel_id=self._channel.id, thread_external_id=None,
                    supports_threads=False)
                kb = await self.build_agent_picker_keyboard(chat_id=chat.id)
                if self._app is not None:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                         for b in row] for row in kb])
                    await self._app.bot.send_message(
                        chat_id=self._channel.external_id,
                        text="Pick an agent:", reply_markup=markup)
                    return None
                return "Reply with /agent <agent-id> to switch."
            return None
        gate_inbox = ChatResponseInbox(
            storage_provider=self._sp, event_bus=self._bus,
            claim_engine=self._claim_engine)
        router = ChatChannelRouter(
            storage_provider=self._sp, event_bus=self._bus, gate_inbox=gate_inbox,
            claim_engine=self._claim_engine)
        await router.deliver_message(
            channel_id=self._channel.id, thread_external_id=None,
            supports_threads=False, sender_name=sender_name, text=text)
        return None

    async def _extract_media_parts(self, msg) -> tuple[list, str]:
        """Download every attachment on ``msg`` and build artifact-backed
        chat media parts. Returns ``(parts, skipped_note)`` where
        ``skipped_note`` is a short suffix to append to the caption when one
        or more attachments were rejected (too large / disallowed type).

        Media is skipped entirely (empty parts) when the adapter has no
        artifact registry wired (text-only deployments)."""
        if self._artifacts is None or self._app is None:
            return [], ""
        from primer.channel.media import (
            MediaError, store_inbound_media,
        )

        store = await self._artifacts.get_default()

        # (file_id, mime_type, filename) for each attachment on the message.
        attachments: list[tuple[str, str | None, str | None]] = []
        photo = getattr(msg, "photo", None)
        if photo:
            # PhotoSize list is ascending by resolution; take the highest.
            attachments.append((photo[-1].file_id, "image/jpeg", None))
        document = getattr(msg, "document", None)
        if document is not None:
            attachments.append((
                document.file_id,
                getattr(document, "mime_type", None),
                getattr(document, "file_name", None),
            ))
        audio = getattr(msg, "audio", None)
        if audio is not None:
            attachments.append((
                audio.file_id, getattr(audio, "mime_type", None), None))
        voice = getattr(msg, "voice", None)
        if voice is not None:
            attachments.append((voice.file_id, "audio/ogg", None))
        video = getattr(msg, "video", None)
        if video is not None:
            attachments.append((
                video.file_id, getattr(video, "mime_type", None), None))

        parts: list = []
        skipped = 0
        for file_id, mime, filename in attachments:
            try:
                tg_file = await self._app.bot.get_file(file_id)
                data = bytes(await tg_file.download_as_bytearray())
                part = await store_inbound_media(
                    store, data=data, mime_type=mime, filename=filename,
                    config=self._media_config,
                )
                parts.append(part)
            except MediaError:
                skipped += 1
            except Exception:  # noqa: BLE001 — one bad attachment must not drop the turn
                logger.exception("telegram: media download/store failed")
                skipped += 1
        note = " [attachment skipped: too large]" if skipped else ""
        return parts, note

    async def handle_inbound_chat_media(
        self, *, sender_name: str, msg,
    ) -> str | None:
        """Dispatch an inbound message carrying media. The user text is the
        caption (``msg.caption``; None -> ""). Media attachments are downloaded
        and stored, then the turn is routed through the chat router with the
        caption as the leading TextPart and the media parts following.

        A caption that parses as a command is handled as a text command (media
        is ignored — commands are text-only)."""
        if self._sp is None:
            return None
        caption = getattr(msg, "caption", None) or ""
        from primer.channel.commands import parse_command
        if parse_command(caption) is not None:
            return await self.handle_inbound_chat_text(
                sender_name=sender_name, text=caption)

        parts, note = await self._extract_media_parts(msg)
        text = caption + note if (caption or note) else ""

        from primer.channel.chat_inbox import ChatResponseInbox
        from primer.channel.chat_router import ChatChannelRouter
        gate_inbox = ChatResponseInbox(
            storage_provider=self._sp, event_bus=self._bus,
            claim_engine=self._claim_engine)
        router = ChatChannelRouter(
            storage_provider=self._sp, event_bus=self._bus,
            gate_inbox=gate_inbox, claim_engine=self._claim_engine)
        await router.deliver_message(
            channel_id=self._channel.id, thread_external_id=None,
            supports_threads=False, sender_name=sender_name, text=text,
            media_parts=parts)
        return None

    async def build_agent_picker_keyboard(
        self, *, chat_id: str, page: int = 0,
    ) -> list[list[dict[str, str]]]:
        """One inline button per agent for the current page, plus a Prev/Next
        nav row when there is more than one page."""
        from primer.channel.commands import CommandExecutor
        res = await CommandExecutor(storage_provider=self._sp).agent_picker()
        items = res.items
        total = len(items)
        pages = max(1, (total + _AGENTS_PER_PAGE - 1) // _AGENTS_PER_PAGE)
        page = max(0, min(page, pages - 1))
        start = page * _AGENTS_PER_PAGE
        rows: list[list[dict[str, str]]] = []
        for opt in items[start:start + _AGENTS_PER_PAGE]:
            rows.append([{
                "text": opt["label"],
                "callback_data": f"pick_agent:{chat_id}:{opt['agent_id']}",
            }])
        nav: list[dict[str, str]] = []
        if page > 0:
            nav.append({"text": "< Prev",
                        "callback_data": f"agentpage:{chat_id}:{page - 1}"})
        if page < pages - 1:
            nav.append({"text": "Next >",
                        "callback_data": f"agentpage:{chat_id}:{page + 1}"})
        if nav:
            rows.append(nav)
        return rows

    async def apply_agent_pick(self, *, callback_data: str) -> str:
        """Handle a 'pick_agent:<chat>:<agent>' callback. Returns a notice."""
        from primer.channel.commands import CommandExecutor
        _, chat_id, agent_id = callback_data.split(":", 2)
        res = await CommandExecutor(storage_provider=self._sp).set_agent(
            chat_id=chat_id, agent_id=agent_id)
        return res.text or "Agent switched."

    async def apply_chat_decision_button(self, *, callback_data: str) -> None:
        """Handle 'chat_ok:<chat>' / 'chat_no:<chat>' approval buttons."""
        from primer.channel.chat_inbox import ChatResponseInbox
        verb, chat_id = callback_data.split(":", 1)
        chat = await self._sp.get_storage(Chat).get(chat_id)
        if chat is None or chat.pending_tool_call is None:
            return
        decision = "approved" if verb == "chat_ok" else "rejected"
        inbox = ChatResponseInbox(
            storage_provider=self._sp, event_bus=self._bus,
            claim_engine=self._claim_engine)
        await inbox.handle_chat_decision(
            chat_id=chat_id, pending=chat.pending_tool_call,
            decision=decision, reason=None, sender="telegram")

    async def post_chat_message(self, text: str) -> dict[str, Any]:
        """Outbound chat relay: send a plain message to the channel."""
        if self._app is None:
            raise ProviderError("TelegramChannelAdapter used before initialize()")
        msg = await self._app.bot.send_message(
            chat_id=self._channel.external_id, text=text)
        return {"message_id": getattr(msg, "message_id", 0)}

    async def _post_envelope_media(self, envelope: PromptEnvelope) -> None:
        """Upload any media attached to an ask_user/inform prompt (workspace
        files) to the chat before the prompt text."""
        media = getattr(envelope, "media", None)
        if not media or self._artifacts is None:
            return
        from primer.channel.media import hydrate_media_dicts
        parts = await hydrate_media_dicts(self._artifacts, media)
        if parts:
            await self.post_chat_media(parts)

    async def post_chat_media(
        self, parts: list, *, thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Outbound media relay: upload each hydrated media part (inline bytes)
        to the channel via the matching Telegram send method."""
        import io
        if self._app is None:
            raise ProviderError("TelegramChannelAdapter used before initialize()")
        sent = 0
        for part in parts:
            data = getattr(part, "data", None)
            if not data:
                continue
            mime = (getattr(part, "mime_type", None) or "").lower()
            filename = getattr(part, "filename", None) or "file"
            buf = io.BytesIO(data)
            chat_id = self._channel.external_id
            if mime.startswith("image/"):
                await self._app.bot.send_photo(chat_id=chat_id, photo=buf)
            elif mime.startswith("audio/"):
                await self._app.bot.send_audio(chat_id=chat_id, audio=buf)
            else:
                buf.name = filename
                await self._app.bot.send_document(
                    chat_id=chat_id, document=buf, filename=filename)
            sent += 1
        return {"sent": sent}


__all__ = ["TelegramChannelAdapter"]
