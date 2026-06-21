from __future__ import annotations

from datetime import datetime, timezone

from primer.channel.normalizer import ProviderCapabilities
from primer.model.channel import ChannelProviderType
from primer.model.channel_event import (
    ChannelEvent,
    EventSender,
    NormalizedEventType,
)


class TelegramEventNormalizer:
    def __init__(self, *, provider_id: str) -> None:
        self._provider_id = provider_id

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=ChannelProviderType.TELEGRAM,
            supported={
                NormalizedEventType.MESSAGE_POSTED,
                NormalizedEventType.COMMAND_INVOKED,
                NormalizedEventType.COMPONENT_ACTED,
            },
            prerequisites={
                "privacy_mode": (
                    "disable BotFather privacy mode (or make the bot a group "
                    "admin) to receive group messages"
                ),
            },
        )

    async def normalize(self, raw) -> ChannelEvent | None:
        kind = raw.get("type")
        p = raw.get("payload") or {}

        if kind == "message":
            chat = p.get("chat") or {}
            surface = "dm" if chat.get("type") == "private" else "channel"
            sender = p.get("from") or {}
            text = p.get("text")

            entities = p.get("entities") or []
            command_entity = next(
                (
                    e
                    for e in entities
                    if e.get("type") == "bot_command" and e.get("offset", 0) == 0
                ),
                None,
            )

            event_type = NormalizedEventType.MESSAGE_POSTED
            command = None
            if command_entity is not None and text:
                length = command_entity.get("length", 0)
                token = text[1:length]
                name = token.split("@", 1)[0]
                remainder = text[length:]
                command = {"name": name, "args": remainder.strip()}
                event_type = NormalizedEventType.COMMAND_INVOKED

            return ChannelEvent(
                provider=ChannelProviderType.TELEGRAM,
                provider_id=self._provider_id,
                event_id=str(p.get("message_id")),
                type=event_type,
                occurred_at=datetime.now(timezone.utc),
                room_external_id=str(chat.get("id")),
                surface=surface,
                message_id=str(p.get("message_id")),
                sender=EventSender(
                    external_id=str(sender.get("id") or ""),
                    display_name=sender.get("full_name"),
                ),
                text=text,
                command=command,
                media=[],
                raw=p,
            )

        if kind == "callback_query":
            message = p.get("message") or {}
            chat = message.get("chat") or {}
            surface = "dm" if chat.get("type") == "private" else "channel"
            sender = p.get("from") or {}
            return ChannelEvent(
                provider=ChannelProviderType.TELEGRAM,
                provider_id=self._provider_id,
                event_id=str(p.get("id") or ""),
                type=NormalizedEventType.COMPONENT_ACTED,
                occurred_at=datetime.now(timezone.utc),
                room_external_id=str(chat.get("id")),
                surface=surface,
                sender=EventSender(external_id=str(sender.get("id") or "")),
                component={"id": p.get("id"), "value": p.get("data")},
                raw=p,
            )

        return None


__all__ = ["TelegramEventNormalizer"]
