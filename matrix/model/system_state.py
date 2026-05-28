"""Pydantic model for the ``system_state`` singleton table."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SystemState(BaseModel):
    """Single-row state record keyed on ``'singleton'``.

    ``bootstrap_completed_at IS NULL`` means the system has never been
    bootstrapped.  ``IS NOT NULL`` means the first-run bootstrap finished
    successfully.  ``schema_version`` and ``last_migration_at`` are
    reserved for future schema-migration bookkeeping.
    """

    id: str = "singleton"
    bootstrap_completed_at: datetime | None = None
    schema_version: int = 1
    last_migration_at: datetime | None = None
