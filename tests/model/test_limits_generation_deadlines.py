"""D-A3-04: Limits must reject a provider with every generation deadline off.

request_timeout_seconds (the per-event stall window) and
total_timeout_seconds (the whole-generation ceiling) are the two
generation deadlines: both None leaves a generation with no bound at all
(the P5 profile rode exactly this shape with connect also null, and the
only thing standing between a wedged upstream and an infinite call was
the SDK default). connect_timeout_seconds is not a generation deadline:
it bounds OPENING the stream, and a cold load is deliberately never
aborted by it, so it may stay None.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from primer.model.provider import Limits


def test_both_generation_deadlines_off_is_rejected() -> None:
    with pytest.raises(ValidationError, match="deadline"):
        Limits(max_concurrency=1, request_timeout_seconds=None, total_timeout_seconds=None)


def test_stall_off_is_fine_when_the_total_budget_stays_on() -> None:
    lim = Limits(max_concurrency=1, request_timeout_seconds=None, total_timeout_seconds=60.0)
    assert lim.request_timeout_seconds is None
    assert lim.total_timeout_seconds == 60.0


def test_total_off_is_fine_when_the_stall_window_stays_on() -> None:
    lim = Limits(max_concurrency=1, request_timeout_seconds=60.0, total_timeout_seconds=None)
    assert lim.request_timeout_seconds == 60.0
    assert lim.total_timeout_seconds is None


def test_the_defaults_stay_valid() -> None:
    lim = Limits(max_concurrency=1)
    assert lim.request_timeout_seconds == 300.0
    assert lim.total_timeout_seconds is None


def test_connect_none_never_invalidates_a_row() -> None:
    lim = Limits(max_concurrency=1, connect_timeout_seconds=None, request_timeout_seconds=60.0)
    assert lim.connect_timeout_seconds is None
