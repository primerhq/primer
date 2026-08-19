"""Client attachment rows: which browsers currently hold a session open.

An attachment is created when a client opens a session and refreshed by a
heartbeat; it expires ``ATTACH_TTL_SECONDS`` after the last refresh so a
silently-dead browser stops counting. A turn started while at least one
attachment is live carries the client toolset (S3 spec section 4).

``attached_seq`` is the session's high-water seq AT ATTACH TIME and never
moves on refresh: it is the replay fence the browser dispatcher uses to
tell a fresh delivery record (execute) from a replayed one (render only).
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import Field

from primer.model.common import Identifiable


class ClientAttachment(Identifiable):
    """One live client attachment to one session."""

    _id_prefix: ClassVar[str | None] = "att"

    workspace_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    client_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Opaque per-tab client identifier. Re-attaching with the same "
            "id is the heartbeat."
        ),
    )
    attached_seq: int = Field(
        default=0,
        ge=0,
        description=(
            "Session ``last_seq`` observed at attach time. The replay "
            "fence; never moved by a heartbeat."
        ),
    )
    expires_at: datetime = Field(
        ..., description="TTL deadline; past this the attachment is dead."
    )
    created_at: datetime = Field(...)


__all__ = ["ClientAttachment"]
