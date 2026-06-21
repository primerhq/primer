from __future__ import annotations

from datetime import datetime, timezone

from primer.channel.normalizer import ProviderCapabilities
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)


class DiscordEventNormalizer:
    def __init__(self, *, provider_id: str) -> None:
        self._provider_id = provider_id

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=ChannelProviderType.DISCORD,
            supported={
                NormalizedEventType.MESSAGE_POSTED,
                NormalizedEventType.COMMAND_INVOKED,
                NormalizedEventType.COMPONENT_ACTED,
            },
            prerequisites={
                "message_content_intent": (
                    "enable the MESSAGE CONTENT privileged intent in the "
                    "Developer Portal"
                )
            },
        )

    async def normalize(self, raw) -> ChannelEvent | None:
        kind = raw.get("type")
        p = raw.get("payload") or {}

        if kind == "message":
            author = p.get("author") or {}
            if author.get("bot"):
                return None
            ch = p.get("channel") or {}
            ch_kind = ch.get("kind")
            surface = (
                "thread"
                if ch_kind == "thread"
                else ("dm" if ch_kind == "dm" else "channel")
            )
            thread_anchor = (
                str(ch.get("id")) if ch_kind == "thread" else None
            )
            room_external_id = (
                str(ch.get("parent_id"))
                if ch_kind == "thread"
                else str(ch.get("id"))
            )
            return ChannelEvent(
                provider=ChannelProviderType.DISCORD,
                provider_id=self._provider_id,
                event_id=str(p.get("id")),
                type=NormalizedEventType.MESSAGE_POSTED,
                occurred_at=datetime.now(timezone.utc),
                room_external_id=room_external_id,
                surface=surface,
                thread_anchor=thread_anchor,
                message_id=str(p.get("id")),
                sender=EventSender(
                    external_id=str(author.get("id") or ""),
                    display_name=author.get("display_name")
                    or author.get("name"),
                    is_bot=bool(author.get("bot")),
                ),
                text=p.get("content"),
                raw=p,
            )

        if kind == "application_command":
            ch = p.get("channel") or {}
            ch_kind = ch.get("kind")
            surface = (
                "thread"
                if ch_kind == "thread"
                else ("dm" if ch_kind == "dm" else "channel")
            )
            thread_anchor = (
                str(ch.get("id")) if ch_kind == "thread" else None
            )
            room_external_id = (
                str(ch.get("parent_id"))
                if ch_kind == "thread"
                else str(ch.get("id"))
            )
            return ChannelEvent(
                provider=ChannelProviderType.DISCORD,
                provider_id=self._provider_id,
                event_id=str(p.get("interaction_id") or p.get("id") or ""),
                type=NormalizedEventType.COMMAND_INVOKED,
                occurred_at=datetime.now(timezone.utc),
                room_external_id=room_external_id,
                surface=surface,
                thread_anchor=thread_anchor,
                command={"name": p.get("name"), "args": p.get("options") or {}},
                sender=EventSender(
                    external_id=str((p.get("user") or {}).get("id") or "")
                ),
                raw=p,
            )

        return None


__all__ = ["DiscordEventNormalizer"]
