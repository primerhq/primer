"""Internal decision for one workspace slot allocation. Contains no transcript or credentials."""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field

from primer.model.common import Identifiable


class SessionCreationIntent(Identifiable):
    """Keyed by the reserved child ID. An abandoned ID is never reused."""

    workspace_id: str
    occurrence: str = Field(default_factory=lambda: uuid4().hex)
    state: Literal["preparing", "committed", "abandoned"] = "preparing"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
