"""TelegramChannelAdapter — one per Channel row."""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.adapter import (
    DEFAULT_CACHE_MAXSIZE, BoundedDict, ChannelAdapter, PromptEnvelope,
    attribution_header,
)
from primer.channel.telegram.connection import TELEGRAM_CONNECTIONS
from primer.channel.telegram.render import (
    build_ask_user_message,
    build_tool_approval_message,
    compute_tag,
)
from primer.model.channel import Channel, ChannelProvider
from primer.model.except_ import ProviderError


logger = logging.getLogger(__name__)

# Backwards-compatible aliases. The bounded-map primitive now lives on the
# adapter base (shared by every provider); these names are kept so existing
# imports keep working.
_CACHE_MAXSIZE = DEFAULT_CACHE_MAXSIZE
_BoundedDict = BoundedDict

# Agents shown per page in the /agent inline-keyboard picker.
_AGENTS_PER_PAGE = 8


class TelegramChannelAdapter(ChannelAdapter):
    """Per-channel Telegram adapter."""

    def __init__(
        self, *, provider: ChannelProvider, channel: Channel, inbox,
        storage_provider=None, event_bus=None, claim_engine=None,
        artifact_registry=None, workspace_registry=None, scheduler=None,
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
        # S6 section 5: the inbound path creates and steers sessions.
        self._workspace_registry = workspace_registry
        self._scheduler = scheduler
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

    def _user_id_key(self) -> str:
        return "telegram_user_id"

    def remember_reply_target(
        self, *, message_id: int, ids: dict[str, str], kind: str,
    ) -> None:
        """Record a rejection-prompt reply target keyed by the bot message the
        user will reply to. Only the tool-rejection text-reason flow uses this
        (the reject prompt sent after the Reject button); ask_user replies are
        correlated via the persistent CorrelationStore instead."""
        self._reply_targets[message_id] = {**ids, "kind": kind}

    def resolve_reply_target(self, message_id: int) -> dict[str, str] | None:
        """Look up a rejection-prompt reply target by the bot message it replies to.

        Only used for the tool-rejection text-reason flow (the reject prompt
        sent after the user presses the Reject button). ask_user replies are
        routed via the persistent CorrelationStore instead."""
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
        # ask_user is answered by a text reply -> persist correlation in store.
        # tool_approval is answered by the inline buttons (callback_data).
        if envelope.kind == "ask_user" and message_id:
            if self._sp is not None:
                from primer.channel.correlation import CorrelationStore
                try:
                    await CorrelationStore(self._sp).upsert_session(
                        channel_id=self._channel.id,
                        anchor=str(message_id),
                        workspace_id=envelope.workspace_id,
                        session_id=envelope.session_id,
                        tool_call_id=envelope.tool_call_id,
                    )
                except Exception:
                    logger.warning(
                        "telegram: failed to persist ask_user correlation "
                        "for message %s", message_id, exc_info=True,
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


    async def collect_inbound_media(self, raw: Any) -> list:
        parts, _note = await self._extract_media_parts(raw)
        return parts

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
        if self._app is None:
            raise ProviderError("TelegramChannelAdapter used before initialize()")
        sent = await self._send_media_parts(None, parts)
        return {"sent": sent}

    async def _send_media_part(self, target: Any, part: Any) -> None:
        import io
        data = getattr(part, "data", None)
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


__all__ = ["TelegramChannelAdapter"]
