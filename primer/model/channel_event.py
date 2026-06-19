from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from primer.model.channel import ChannelProviderType


class NormalizedEventType(str, Enum):
    MESSAGE_POSTED = "message.posted"
    COMMAND_INVOKED = "command.invoked"
    COMPONENT_ACTED = "component.acted"
    REACTION_ADDED = "reaction.added"
    REACTION_REMOVED = "reaction.removed"
    MESSAGE_EDITED = "message.edited"
    MEMBER_JOINED = "member.joined"
    BOT_INSTALLED = "bot.installed"
    BOT_REMOVED = "bot.removed"
    ROOM_CREATED = "room.created"


class EventSender(BaseModel):
    external_id: str
    display_name: str | None = None
    roles: list[str] = Field(default_factory=list)
    is_bot: bool = False


class ChannelEvent(BaseModel):
    provider: ChannelProviderType
    provider_id: str
    event_id: str
    type: NormalizedEventType
    occurred_at: datetime
    room_external_id: str | None = None
    channel_id: str | None = None
    surface: Literal["dm", "channel", "thread"]
    thread_anchor: str | None = None
    message_id: str | None = None
    sender: EventSender
    text: str | None = None
    mentions_bot: bool = False
    command: dict | None = None
    component: dict | None = None
    reaction: dict | None = None
    media: list = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


__all__ = ["ChannelEvent", "EventSender", "NormalizedEventType"]
