from __future__ import annotations

from datetime import datetime, timezone

from primer.channel.normalizer import ProviderCapabilities
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)


class SlackEventNormalizer:
    def __init__(self, *, provider_id: str) -> None:
        self._provider_id = provider_id

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=ChannelProviderType.SLACK,
            supported={
                NormalizedEventType.MESSAGE_POSTED,
                NormalizedEventType.COMMAND_INVOKED,
            },
            prerequisites={
                "scopes": "bot needs chat:write, channels:read, channels:history",
                "event_subscriptions": "subscribe to message.channels and app_mention",
            },
        )

    async def normalize(self, raw) -> ChannelEvent | None:
        kind = raw.get("type")
        p = raw.get("payload") or {}

        if kind in ("message", "app_mention"):
            if p.get("bot_id"):
                return None
            subtype = p.get("subtype")
            if subtype and subtype != "file_share":
                return None
            return ChannelEvent(
                provider=ChannelProviderType.SLACK,
                provider_id=self._provider_id,
                event_id=p.get("ts") or "",
                type=NormalizedEventType.MESSAGE_POSTED,
                occurred_at=datetime.now(timezone.utc),
                room_external_id=p.get("channel"),
                surface="thread" if p.get("thread_ts") else "channel",
                thread_anchor=p.get("thread_ts"),
                message_id=p.get("ts"),
                sender=EventSender(external_id=p.get("user") or ""),
                text=p.get("text"),
                # The factory computes the flag from the message text (the
                # bot user named as <@BOT_USER_ID>); the normalizer only
                # reads it. The app_mention event is never routed (Slack
                # sends it AND the message event for one mention), so the
                # kind no longer decides the flag.
                mentions_bot=bool(p.get("mentions_bot")),
                raw=p,
            )

        if kind == "slash_command":
            return ChannelEvent(
                provider=ChannelProviderType.SLACK,
                provider_id=self._provider_id,
                event_id=p.get("trigger_id") or p.get("command") or "",
                type=NormalizedEventType.COMMAND_INVOKED,
                occurred_at=datetime.now(timezone.utc),
                room_external_id=p.get("channel_id"),
                surface="channel",
                sender=EventSender(external_id=p.get("user_id") or ""),
                command={
                    "name": (p.get("command") or "").lstrip("/"),
                    "args": p.get("text") or "",
                },
                text=p.get("text"),
                raw=p,
            )

        return None


__all__ = ["SlackEventNormalizer"]
