"""Cron helpers — Spec §4.2, §8."""

from __future__ import annotations
from datetime import datetime, timezone, timedelta

import pytest

from primer.trigger.cron import (
    next_fire_at, iter_missed_fires, validate_cron, validate_timezone,
    CronInvalid, TimezoneInvalid,
)


def test_next_fire_at_utc():
    base = datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc)
    nxt = next_fire_at("0 9 * * *", "UTC", after=base)
    assert nxt == datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def test_next_fire_at_in_iana_timezone():
    # 9am Asia/Dubai = 5am UTC (UTC+4, no DST)
    base = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)
    nxt = next_fire_at("0 9 * * *", "Asia/Dubai", after=base)
    assert nxt == datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)


def test_validate_cron_rejects_garbage():
    with pytest.raises(CronInvalid):
        validate_cron("not a cron")


def test_validate_timezone_rejects_garbage():
    with pytest.raises(TimezoneInvalid):
        validate_timezone("Not/A_Zone")


def test_iter_missed_fires_one():
    # Cron every hour; base = now - 3 hours
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    base = now - timedelta(hours=3, minutes=5)
    fires = list(iter_missed_fires("0 * * * *", "UTC", from_=base, now=now, limit=64))
    # Expect: 09:00, 10:00, 11:00, 12:00 (4 missed)
    assert len(fires) == 4
    assert fires[0] == datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    assert fires[-1] == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_iter_missed_fires_respects_limit():
    now = datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc)
    base = now - timedelta(days=10)
    fires = list(iter_missed_fires("0 * * * *", "UTC", from_=base, now=now, limit=5))
    assert len(fires) == 5
