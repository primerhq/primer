"""Deterministic fire-id generator — Spec §12.6."""

from __future__ import annotations
from datetime import datetime


def make_fire_id(trigger_id: str, instant: datetime) -> str:
    """Return ``f'fire-{trigger_id}-{ms_since_epoch}'`` for idempotent ref.

    ``instant`` is the LOGICAL fire instant: a scheduled trigger passes
    its ``scheduled_for`` tick so that an at-least-once redelivery of the
    same tick resolves to the same ``fire_id`` (and is deduped). Callers
    without a logical instant (one-off / event fires) pass ``fired_at``.
    """
    ms = int(instant.timestamp() * 1000)
    return f"fire-{trigger_id}-{ms}"


__all__ = ["make_fire_id"]
