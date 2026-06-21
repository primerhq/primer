from __future__ import annotations

import re

from pydantic import BaseModel

from primer.model.channel_event import ChannelEvent, NormalizedEventType


class EventMatcher(BaseModel):
    event_type: NormalizedEventType
    surface: list[str] | None = None
    room_external_ids: list[str] | None = None
    command_name: str | None = None
    mentions_bot: bool | None = None
    sender_roles_any: list[str] | None = None
    sender_ids_any: list[str] | None = None
    text_pattern: str | None = None


def matches(matcher: EventMatcher, event: ChannelEvent) -> bool:
    if event.type != matcher.event_type:
        return False
    if matcher.surface is not None and event.surface not in matcher.surface:
        return False
    if (
        matcher.room_external_ids is not None
        and event.room_external_id not in matcher.room_external_ids
    ):
        return False
    if matcher.command_name is not None and not (
        event.command is not None
        and event.command.get("name") == matcher.command_name
    ):
        return False
    if matcher.mentions_bot is not None and event.mentions_bot != matcher.mentions_bot:
        return False
    if matcher.sender_roles_any is not None and not (
        set(event.sender.roles) & set(matcher.sender_roles_any)
    ):
        return False
    if (
        matcher.sender_ids_any is not None
        and event.sender.external_id not in matcher.sender_ids_any
    ):
        return False
    if matcher.text_pattern is not None and not (
        event.text is not None and re.search(matcher.text_pattern, event.text) is not None
    ):
        return False
    return True


__all__ = ["EventMatcher", "matches"]
