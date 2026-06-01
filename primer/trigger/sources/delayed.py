"""Delayed (one-off) trigger source — Spec §4.1."""

from __future__ import annotations

from datetime import datetime

from primer.model.trigger import Trigger


class DelayedSource:
    """Source for kind='delayed' triggers.

    Fires once at config.fire_at, then auto-disables (caller flips enabled=False
    after release).
    """

    kind = "delayed"
    eligible_for_claim = True

    def compute_next_fire_at(
        self,
        trigger: Trigger,
        *,
        now: datetime,
    ) -> datetime | None:
        # One-off: if it's already fired (last_fired_at set), there's no next.
        if trigger.last_fired_at is not None:
            return None
        return trigger.config.fire_at

    def build_fire_context(
        self,
        trigger: Trigger,
        *,
        fired_at: datetime,
        scheduled_for: datetime | None = None,
    ) -> dict:
        return {
            "trigger_id": trigger.id,
            "trigger_slug": trigger.slug,
            "kind": "delayed",
            "fired_at": fired_at.isoformat(),
            "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
        }


__all__ = ["DelayedSource"]
