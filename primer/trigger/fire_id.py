"""Deterministic fire-id generator — Spec §12.6."""

from __future__ import annotations
from datetime import datetime


def make_fire_id(trigger_id: str, fired_at: datetime) -> str:
    """Return ``f'fire-{trigger_id}-{ms_since_epoch}'`` for idempotent ref."""
    ms = int(fired_at.timestamp() * 1000)
    return f"fire-{trigger_id}-{ms}"


__all__ = ["make_fire_id"]
