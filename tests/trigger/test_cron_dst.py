"""A fixed-time cron fires once a day across a clock change (review of workspaces, W11).

When clocks fall back, 01:30 happens twice (EDT then EST). Classic cron runs a job with a fixed hour once; a wildcard-hour job (hourly) runs in both
passes. When clocks spring forward, a time that does not exist runs once, at the shifted time. ``next_fire_at``, ``iter_missed_fires``,
``previous_fire_at`` and the catch-up built on them all agree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from primer.trigger.cron import iter_missed_fires, next_fire_at, previous_fire_at, scheduled_ticks_to_fire

NY = "America/New_York"


def _z(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _walk(expr: str, tz: str, start: datetime, stop: datetime) -> list[datetime]:
    out, tick = [], next_fire_at(expr, tz, after=start)
    while tick <= stop:
        out.append(tick)
        tick = next_fire_at(expr, tz, after=tick)
    return out


def test_a_daily_fixed_time_fires_once_when_clocks_fall_back():
    ticks = _walk("30 1 * * *", NY, _z(2026, 10, 31, 0), _z(2026, 11, 3, 0))

    assert ticks == [_z(2026, 10, 31, 5, 30), _z(2026, 11, 1, 5, 30), _z(2026, 11, 2, 6, 30)], "01:30 EST of Nov 1 (06:30Z) must not fire again"


def test_the_next_tick_from_inside_the_repeated_hour_is_tomorrows():
    # 06:10Z is 01:10 EST, the second pass: today's 01:30 already ran at 05:30Z.
    assert next_fire_at("30 1 * * *", NY, after=_z(2026, 11, 1, 6, 10)) == _z(2026, 11, 2, 6, 30)
    assert next_fire_at("30 1 * * *", NY, after=_z(2026, 11, 1, 5, 45)) == _z(2026, 11, 2, 6, 30)


def test_a_wildcard_hour_job_still_runs_in_both_passes():
    ticks = _walk("0 * * * *", NY, _z(2026, 11, 1, 4, 30), _z(2026, 11, 1, 7, 30))

    assert ticks == [_z(2026, 11, 1, 5), _z(2026, 11, 1, 6), _z(2026, 11, 1, 7)], "01:00 EDT and 01:00 EST are both hourly ticks"
    stepped = _walk("*/30 * * * *", NY, _z(2026, 11, 1, 5, 15), _z(2026, 11, 1, 7, 0))
    assert _z(2026, 11, 1, 6, 30) in stepped


def test_a_fixed_time_outside_the_repeated_hour_is_untouched():
    assert _walk("30 3 * * *", NY, _z(2026, 10, 31, 0), _z(2026, 11, 3, 12)) == [
        _z(2026, 10, 31, 7, 30), _z(2026, 11, 1, 8, 30), _z(2026, 11, 2, 8, 30), _z(2026, 11, 3, 8, 30),
    ]


def test_a_fixed_time_in_a_zone_without_dst_is_untouched():
    assert _walk("30 1 * * *", "UTC", _z(2026, 10, 31, 0), _z(2026, 11, 3, 0)) == [_z(2026, 10, 31, 1, 30), _z(2026, 11, 1, 1, 30), _z(2026, 11, 2, 1, 30)]


def test_spring_forward_fires_a_skipped_time_once_at_the_shifted_time():
    ticks = _walk("30 2 * * *", NY, _z(2026, 3, 7, 12), _z(2026, 3, 10, 12))

    assert ticks == [_z(2026, 3, 8, 7, 0), _z(2026, 3, 9, 6, 30), _z(2026, 3, 10, 6, 30)], "02:30 does not exist on Mar 8: once, at 03:00 EDT; the next days are normal"


def test_previous_tick_skips_the_second_pass():
    assert previous_fire_at("30 1 * * *", NY, at_or_before=_z(2026, 11, 1, 6, 45)) == _z(2026, 11, 1, 5, 30)
    assert previous_fire_at("30 1 * * *", NY, at_or_before=_z(2026, 11, 1, 6, 30)) == _z(2026, 11, 1, 5, 30)
    assert previous_fire_at("0 * * * *", NY, at_or_before=_z(2026, 11, 1, 6, 45)) == _z(2026, 11, 1, 6, 0)


def test_the_missed_tick_walk_lists_each_day_once():
    missed = list(iter_missed_fires("30 1 * * *", NY, from_=_z(2026, 10, 31, 0), now=_z(2026, 11, 2, 12)))

    assert missed == [_z(2026, 10, 31, 5, 30), _z(2026, 11, 1, 5, 30), _z(2026, 11, 2, 6, 30)]


@pytest.mark.parametrize("catchup, expected", [
    ("all", [_z(2026, 10, 31, 5, 30), _z(2026, 11, 1, 5, 30), _z(2026, 11, 2, 6, 30)]),
    ("one", [_z(2026, 11, 1, 5, 30), _z(2026, 11, 2, 6, 30)]),
    ("none", [_z(2026, 11, 2, 6, 30)]),
])
def test_catch_up_after_downtime_does_not_double_count_the_repeated_hour(catchup, expected):
    ticks = scheduled_ticks_to_fire(
        "30 1 * * *", NY, catchup=catchup, first_unfired=_z(2026, 10, 31, 5, 30), now=_z(2026, 11, 2, 6, 30) + timedelta(seconds=1),
    )

    assert ticks == expected
