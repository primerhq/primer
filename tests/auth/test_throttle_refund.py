"""``LoginThrottle.refund``: an attempt that turned out fine gives its count back (the per-address budget counts failures only)."""

from __future__ import annotations

from primer.auth.throttle import LoginThrottle


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _throttle(clock, **kw) -> LoginThrottle:
    return LoginThrottle(free_attempts=3, base_delay=2.0, clock=clock, **kw)


def test_a_refunded_attempt_does_not_count_toward_the_free_attempts():
    t = _throttle(_Clock())
    for _ in range(10):
        assert t.reserve("", "addr") == 0
        t.refund("", "addr")

    assert t.reserve("", "addr") == 0 and len(t) == 1


def test_failures_still_accumulate_between_refunds():
    t = _throttle(_Clock())
    assert t.reserve("", "addr") == 0  # failure 1 (kept)
    assert t.reserve("", "addr") == 0  # failure 2 (kept)
    assert t.reserve("", "addr") == 0  # failure 3 (kept): the next one waits
    assert t.reserve("", "addr") >= 1


def test_a_refund_after_the_wait_began_lifts_it_when_the_count_drops_below_the_free_attempts():
    t = _throttle(_Clock())
    for _ in range(3):
        t.reserve("", "addr")  # the 3rd sets a wait
    t.refund("", "addr")  # the 3rd turned out to be a success

    assert t.reserve("", "addr") == 0


def test_refunding_an_unknown_key_or_below_zero_is_a_no_op():
    t = _throttle(_Clock())
    t.refund("", "never-seen")
    t.reserve("", "addr")
    t.refund("", "addr")
    t.refund("", "addr")

    assert t.reserve("", "addr") == 0
