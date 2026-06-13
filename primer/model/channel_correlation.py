from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import Field

from primer.model.common import Identifiable


class ChannelCorrelation(Identifiable):
    """Persistent routing record: (channel_id, anchor) -> a chat or a session gate."""

    _id_prefix: ClassVar[str] = "channel-correlation"

    channel_id: str = Field(..., description="Room-Channel id.")
    anchor: str = Field(
        ...,
        description="Thread id (Slack/Discord) | gate message id (Telegram) | '__active_chat__'.",
    )
    kind: Literal["chat", "session"] = Field(...)
    chat_id: str | None = Field(default=None, description="kind=chat.")
    workspace_id: str | None = Field(default=None, description="kind=session.")
    session_id: str | None = Field(default=None, description="kind=session.")
    tool_call_id: str | None = Field(
        default=None, description="kind=session: the currently-pending gate."
    )
    updated_at: datetime | None = Field(default=None)


__all__ = ["ChannelCorrelation"]
