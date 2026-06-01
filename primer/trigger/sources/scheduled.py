"""Scheduled (cron) trigger source — Spec §4.2, §8."""

from __future__ import annotations

from datetime import datetime

from primer.model.trigger import Trigger
from primer.trigger.cron import next_fire_at


class ScheduledSource:
    """Source for kind='scheduled' triggers.

    Recurring cron-based fires. catchup policy (one|all|none) controls how
    missed ticks are handled after downtime; the dispatcher uses
    ``iter_missed_fires`` from ``primer/trigger/cron.py`` to enumerate when
    catchup='all'.
    """

    kind = "scheduled"
    eligible_for_claim = True

    def compute_next_fire_at(
        self,
        trigger: Trigger,
        *,
        now: datetime,
    ) -> datetime | None:
        cfg = trigger.config
        return next_fire_at(cfg.cron, cfg.timezone, after=now)

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
            "kind": "scheduled",
            "fired_at": fired_at.isoformat(),
            "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
        }


__all__ = ["ScheduledSource"]
