from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import Field

from primer.model.common import Identifiable


class ChannelCorrelation(Identifiable):
    """Persistent routing record: (channel_id, anchor) -> a session."""

    _id_prefix: ClassVar[str] = "channel-correlation"

    channel_id: str = Field(..., description="Room-Channel id.")
    anchor: str = Field(
        ...,
        description="Thread id (Slack/Discord) | gate message id (Telegram).",
    )
    kind: Literal["session"] = Field(default="session")
    workspace_id: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    tool_call_id: str | None = Field(
        default=None,
        description=(
            "kind=session: the currently-pending gate, or None when the "
            "record is a plain thread-to-session mapping (S6 section 5)."
        ),
    )
    updated_at: datetime | None = Field(default=None)


__all__ = ["ChannelCorrelation"]
