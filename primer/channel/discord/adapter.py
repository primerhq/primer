"""DiscordChannelAdapter — one per Channel row."""

from __future__ import annotations

import logging
from typing import Any

from primer.channel.adapter import (
    BoundedDict, ChannelAdapter, PromptEnvelope,
    attribution_header, format_tool_args, session_thread_label,
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
        self._client: Any | None = None
        # session_id → discord Thread id (one conversation thread per session).
        # Bounded so a long-lived bot does not grow this map without limit; an
        # evicted session simply re-opens its thread on the next prompt.
        self._session_threads: BoundedDict = BoundedDict()

    def _user_id_key(self) -> str:
        return "discord_user_id"

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
        thread = await self._session_thread(
            channel, envelope.session_id,
            getattr(envelope, "thread_anchor", None),
        )
        await self._post_thread_media(thread, envelope)
        header = attribution_header(envelope)
        if envelope.kind == "tool_approval":
            view = ApprovalView(
                ws=envelope.workspace_id,
                sid=envelope.session_id,
                tcid=envelope.tool_call_id,
            )
            msg = await thread.send(
                content=header + format_approval_content(envelope), view=view,
            )
            return {"message_id": getattr(msg, "id", 0), "thread_id": thread.id}
        elif envelope.kind == "ask_user":
            msg = await thread.send(content=header + envelope.prompt)
            # Persist the correlation so inbound reply routing is durable.
            if self._sp is not None:
                from primer.channel.correlation import CorrelationStore
                try:
                    await CorrelationStore(self._sp).upsert_session(
                        channel_id=self._channel.id,
                        anchor=str(thread.id),
                        workspace_id=envelope.workspace_id,
                        session_id=envelope.session_id,
                        tool_call_id=envelope.tool_call_id,
                    )
                except Exception:
                    logger.warning(
                        "discord: failed to persist ask_user correlation "
                        "for thread %s", thread.id, exc_info=True,
                    )
            return {"message_id": getattr(msg, "id", 0), "thread_id": thread.id}
        elif envelope.kind == "inform":
            msg = await thread.send(content=header + envelope.prompt)
            return {"message_id": getattr(msg, "id", 0), "thread_id": thread.id}
        else:
            raise ProviderError(f"unknown envelope kind {envelope.kind!r}")

    async def _session_thread(
        self, channel: Any, session_id: str, anchor: str | None = None,
    ) -> Any:
        """Get-or-create the one conversation thread for this session.

        The first prompt posts a small anchor message and opens a named thread
        off it; every later prompt for the same session (ask or approval) is
        sent into that thread.

        A thread-mapped session (S6 section 5) supplies its thread id as
        ``anchor``; resolve it instead of opening a new thread.
        """
        if anchor:
            thread = self._client.get_channel(int(anchor))
            if thread is None:
                try:
                    thread = await self._client.fetch_channel(int(anchor))
                except Exception:  # noqa: BLE001 - fall back to the cache
                    thread = None
            if thread is not None:
                self._session_threads[session_id] = thread.id
                return thread
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



    async def post_chat_message(
        self, text: str, *, thread_ts: str | None = None
    ) -> dict[str, Any]:
        """Full-payload outbound relay into the chat's thread."""
        target = await self._resolve_chat_thread(thread_ts)
        await target.send(content=text)
        return {"thread_id": thread_ts}

    async def _post_thread_media(self, thread: Any, envelope: PromptEnvelope) -> None:
        """Upload any media attached to an ask_user/inform prompt (workspace
        files) into the session thread before the prompt text."""
        media = getattr(envelope, "media", None)
        if not media or self._artifacts is None:
            return
        from primer.channel.media import hydrate_media_dicts
        parts = await hydrate_media_dicts(self._artifacts, media)
        await self._send_media_parts(thread, parts)

    async def _send_media_part(self, target: Any, part: Any) -> None:
        import io

        import discord
        data = getattr(part, "data", None)
        filename = getattr(part, "filename", None) or "file"
        await target.send(file=discord.File(io.BytesIO(data), filename=filename))

    async def post_chat_media(
        self, parts: list, *, thread_ts: str | None = None,
    ) -> dict[str, Any]:
        """Outbound media relay: upload each hydrated media part as a file into
        the chat's thread."""
        target = await self._resolve_chat_thread(thread_ts)
        sent = await self._send_media_parts(target, parts)
        return {"sent": sent, "thread_id": thread_ts}


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
        name = f"thread {tid}"
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

    async def collect_inbound_media(self, raw: Any) -> list:
        parts, _text = await self._build_media_parts(
            list(getattr(raw, "attachments", None) or []), "",
        )
        return parts

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



__all__ = ["DiscordChannelAdapter"]
