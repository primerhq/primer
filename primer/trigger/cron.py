"""Cron helpers — timezone-aware croniter wrapper.

Spec §4.2, §8. Pure functions; all I/O-free.
"""

from __future__ import annotations
from datetime import datetime, timezone
from collections.abc import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


class CronInvalid(ValueError):
    pass


class TimezoneInvalid(ValueError):
    pass


def validate_cron(expr: str) -> None:
    """Raise CronInvalid if the expression isn't a valid cron."""
    if not isinstance(expr, str) or not expr.strip():
        raise CronInvalid("cron expression must be a non-empty string")
    if not croniter.is_valid(expr):
        raise CronInvalid(f"invalid cron expression: {expr!r}")


def validate_timezone(tz: str) -> None:
    """Raise TimezoneInvalid if the IANA timezone name is unknown."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezoneInvalid(f"invalid timezone: {tz!r}") from exc


def next_fire_at(
    cron_expr: str,
    tz_name: str,
    *,
    after: datetime,
) -> datetime:
    """Return the next cron occurrence strictly after ``after``, as UTC datetime.

    Cron is evaluated in ``tz_name``; result is converted to UTC.
    """
    validate_cron(cron_expr)
    validate_timezone(tz_name)
    tz = ZoneInfo(tz_name)
    base_local = after.astimezone(tz)
    it = croniter(cron_expr, base_local)
    nxt_local = it.get_next(datetime)
    # croniter returns naive in some versions when given naive input; coerce.
    if nxt_local.tzinfo is None:
        nxt_local = nxt_local.replace(tzinfo=tz)
    return nxt_local.astimezone(timezone.utc)


def iter_missed_fires(
    cron_expr: str,
    tz_name: str,
    *,
    from_: datetime,
    now: datetime,
    limit: int = 64,
) -> Iterator[datetime]:
    """Yield every cron occurrence in (from_, now], up to ``limit`` items.

    Used by catchup='all' to enumerate missed ticks after downtime.
    """
    validate_cron(cron_expr)
    validate_timezone(tz_name)
    tz = ZoneInfo(tz_name)
    base_local = from_.astimezone(tz)
    it = croniter(cron_expr, base_local)
    emitted = 0
    while emitted < limit:
        nxt_local = it.get_next(datetime)
        if nxt_local.tzinfo is None:
            nxt_local = nxt_local.replace(tzinfo=tz)
        nxt_utc = nxt_local.astimezone(timezone.utc)
        if nxt_utc > now:
            return
        yield nxt_utc
        emitted += 1


__all__ = [
    "CronInvalid",
    "TimezoneInvalid",
    "validate_cron",
    "validate_timezone",
    "next_fire_at",
    "iter_missed_fires",
]
