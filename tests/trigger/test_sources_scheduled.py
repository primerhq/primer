"""Scheduled (cron) source — Spec §4.2, §8."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.trigger.sources.scheduled import ScheduledSource
from primer.model.trigger import Trigger, ScheduledTriggerConfig


def _make(cron: str, tz: str = "UTC", catchup: str = "one",
          last_fired_at: datetime | None = None) -> Trigger:
    return Trigger(
        id="tr-1", slug="daily",
        name="Daily", description=None,
        config=ScheduledTriggerConfig(cron=cron, timezone=tz, catchup=catchup),
        enabled=True, next_fire_at=None,
        last_fired_at=last_fired_at,
        created_at=datetime.now(timezone.utc),
    )


def test_scheduled_eligible():
    assert ScheduledSource().eligible_for_claim is True


def test_compute_next_fire_at_uses_cron():
    src = ScheduledSource()
    t = _make("0 9 * * *", tz="UTC")
    now = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)
    nxt = src.compute_next_fire_at(t, now=now)
    assert nxt == datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def test_compute_next_fire_at_respects_timezone():
    src = ScheduledSource()
    t = _make("0 9 * * *", tz="Asia/Dubai")
    now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    nxt = src.compute_next_fire_at(t, now=now)
    # 9am Dubai = 5am UTC
    assert nxt == datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)


def test_build_fire_context_includes_scheduled_for():
    src = ScheduledSource()
    t = _make("0 9 * * *")
    fired_at = datetime(2026, 6, 1, 9, 0, 5, tzinfo=timezone.utc)
    scheduled_for = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    ctx = src.build_fire_context(t, fired_at=fired_at, scheduled_for=scheduled_for)
    assert ctx["kind"] == "scheduled"
    assert ctx["scheduled_for"] == scheduled_for.isoformat()
