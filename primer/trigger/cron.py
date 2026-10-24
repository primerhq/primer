"""Cron helpers — timezone-aware croniter wrapper.

Spec §4.2, §8. Pure functions; all I/O-free.
"""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
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


def previous_fire_at(
    cron_expr: str,
    tz_name: str,
    *,
    at_or_before: datetime,
) -> datetime:
    """Return the latest cron occurrence at or before ``at_or_before``, as UTC datetime.

    Cron is evaluated in ``tz_name``. ``croniter.get_prev`` answers strictly before its start, so the start is nudged one microsecond past
    ``at_or_before``: an instant that is itself a tick is its own answer.
    """
    validate_cron(cron_expr)
    validate_timezone(tz_name)
    tz = ZoneInfo(tz_name)
    base_local = (at_or_before + timedelta(microseconds=1)).astimezone(tz)
    prev_local = croniter(cron_expr, base_local).get_prev(datetime)
    if prev_local.tzinfo is None:
        prev_local = prev_local.replace(tzinfo=tz)
    return prev_local.astimezone(timezone.utc)


#: The most missed ticks one claim of a ``catchup: all`` trigger replays (the oldest first), besides the due tick.
CATCHUP_LIMIT = 64

#: How far a claim's tick may lie ahead of the worker's clock and still be fired: the engine claims on the database's clock, which may run a
#: little ahead of the worker's. A claim whose tick is further ahead is early and fires nothing (ticket 01a16ddb, review of #915).
CLOCK_SKEW = timedelta(seconds=5)


def scheduled_ticks_to_fire(
    cron_expr: str,
    tz_name: str,
    *,
    catchup: str,
    first_unfired: datetime | None,
    now: datetime,
    limit: int = CATCHUP_LIMIT,
    skew: timedelta = CLOCK_SKEW,
) -> list[datetime]:
    """The ticks one claim of a ``scheduled`` trigger fires, oldest first: the missed ticks its ``catchup`` keeps, then the due tick.

    ``first_unfired`` is the trigger's ``next_fire_at``: the first tick no claim has fired. It is computed after each claim's fire, on create
    and on every update, so a trigger enabled again has missed nothing while it was disabled.

    * The due tick is the latest tick at or before the claim: at or before ``now``, or at ``first_unfired`` when the worker's clock reads at
      most ``skew`` before the tick the claim was made for (the engine claims on the database's clock). It fires in every policy, exactly
      once. A claim whose ``first_unfired`` is further ahead than ``skew`` is early: nothing is due, and the answer is ``[]`` (a future tick
      is never fired early).
    * A missed tick is one the platform did not fire when it came: from ``first_unfired`` up to, not including, the due tick.
    * ``none`` fires no missed tick; ``one`` fires the most recent one; ``all`` fires each, at most ``limit``, the oldest first.

    Ticket 01a16ddb (ruling 3): ``all`` used to replay from ``last_fired_at`` THROUGH the due tick and then fire the current tick again (every
    tick twice, with or without downtime), and ``one`` and ``none`` both fired the current tick alone.
    """
    if first_unfired is not None and first_unfired > now + skew:
        return []
    claim_at = now if first_unfired is None else max(now, first_unfired)
    due = previous_fire_at(cron_expr, tz_name, at_or_before=claim_at)
    if first_unfired is None or first_unfired >= due or catchup == "none":
        return [due]
    if catchup == "one":
        latest_missed = previous_fire_at(cron_expr, tz_name, at_or_before=due - timedelta(microseconds=1))
        return [latest_missed, due] if latest_missed >= first_unfired else [due]
    missed: list[datetime] = []
    tick = next_fire_at(cron_expr, tz_name, after=first_unfired - timedelta(microseconds=1))
    while tick < due and len(missed) < limit:
        missed.append(tick)
        tick = next_fire_at(cron_expr, tz_name, after=tick)
    return [*missed, due]


__all__ = [
    "CATCHUP_LIMIT",
    "CLOCK_SKEW",
    "CronInvalid",
    "TimezoneInvalid",
    "validate_cron",
    "validate_timezone",
    "next_fire_at",
    "iter_missed_fires",
    "previous_fire_at",
    "scheduled_ticks_to_fire",
]
