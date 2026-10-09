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
    gate_id: str | None = Field(
        default=None,
        description=(
            "kind=session: the id of the gate ``tool_call_id`` names, as minted when it was created (resume_metadata.gate_id, C-033). A reply "
            "brings it back so a reply to a prompt that has since been replaced under the same tool_call_id is refused. None when there is no "
            "open gate, and for a row written before gates had ids."
        ),
    )
    updated_at: datetime | None = Field(default=None)


__all__ = ["ChannelCorrelation"]
