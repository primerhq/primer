"""System > Activity shows readable times (ADM-32 of the 2026-10-08 admin review).

The OCCURRED column printed the storage format of every event, ``2026-10-07T20:13:43.972680Z``: microseconds, a ``T`` and a ``Z``, in UTC, on every row. The other tables of the
console show a short local time. ``SH_activityTime(iso, prevIso, offsetOf)`` (``shell/sh-activity.jsx``) is pure: ``20:13:43`` for an event on the same LOCAL date as the row above it, and
``7 Oct 20:13:43`` for the first row and wherever the local date changes, so a window that spans midnight is still readable; the full ISO value goes in the cell's ``title``.

The offset is injected (``offsetOf(date)`` returns minutes east of UTC) so the tests do not depend on the machine's time zone, and so each row is converted with the offset OF ITS OWN
DATE (a window that crosses a daylight-saving change must not shift half its rows). The function runs in MiniRacer on the real source; the table is JSX, so how it calls it is a source
check, and ``tests/ui_e2e/test_activity_times_journey.py`` drives the real page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "shell" / "sh-activity.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _time(iso, prev=None, offset: int = 0):
    from py_mini_racer import MiniRacer

    start = SRC.index("function SH_activityTime(")
    end = SRC.index("\n}\n", start) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(SRC[start:end])
    return json.loads(ctx.eval(f"JSON.stringify(SH_activityTime({json.dumps(iso)}, {json.dumps(prev)}, function () {{ return {offset}; }}))"))


def test_the_first_row_shows_its_date_and_a_short_clock() -> None:
    assert _time("2026-10-07T20:13:43.972680Z") == "7 Oct 20:13:43"


def test_a_row_on_the_same_local_date_as_the_one_above_shows_the_clock_only() -> None:
    assert _time("2026-10-07T20:13:44.100000Z", "2026-10-07T20:13:43.972680Z") == "20:13:44"


def test_a_row_on_another_date_shows_the_date_again() -> None:
    assert _time("2026-10-08T00:00:01Z", "2026-10-07T23:59:59Z") == "8 Oct 00:00:01"


def test_no_microseconds_no_t_and_no_z_reach_the_cell() -> None:
    text = _time("2026-10-07T20:13:43.972680Z", "2026-10-07T20:00:00Z")

    assert "." not in text and "T" not in text and "Z" not in text and "972680" not in text


def test_the_clock_is_local_not_utc() -> None:
    """Dubai is UTC+4: 20:13 UTC on the 7th is 00:13 on the 8th."""
    assert _time("2026-10-07T20:13:43Z", None, 240) == "8 Oct 00:13:43"
    assert _time("2026-10-08T02:00:00Z", None, -300) == "7 Oct 21:00:00"


def test_whether_the_date_changed_is_decided_on_local_dates_not_utc_dates() -> None:
    """19:59 UTC and 20:00 UTC are the same UTC day but two different days in UTC+4: the second row shows its date."""
    assert _time("2026-10-07T20:00:00Z", "2026-10-07T19:59:59Z", 240) == "8 Oct 00:00:00"
    assert _time("2026-10-07T18:00:00Z", "2026-10-07T17:00:00Z", 240) == "22:00:00"


def test_a_timestamp_with_no_zone_is_read_as_utc_like_the_api_sends_it() -> None:
    """JS reads a zone-less ISO string as LOCAL time, which would shift every row by the viewer's offset."""
    assert _time("2026-10-07T20:13:43") == _time("2026-10-07T20:13:43Z") == "7 Oct 20:13:43"


@pytest.mark.parametrize("iso", [None, ""])
def test_a_missing_time_is_an_empty_cell(iso) -> None:
    assert _time(iso) == ""


def test_a_value_that_is_not_a_date_is_shown_as_it_is_not_as_nan() -> None:
    assert _time("sometime last week") == "sometime last week"


def test_the_table_prints_the_short_time_with_the_full_value_as_its_tooltip() -> None:
    page = SRC[SRC.index("function SH_ActivityPanel("):]

    cell = re.search(r'<td className="mono" title=\{ev\.occurred_at\}>\s*\{SH_activityTime\(ev\.occurred_at, i \? rows\[i - 1\]\.occurred_at : null, function \(d\) \{ return -d\.getTimezoneOffset\(\); \}\)\}\s*</td>', page)
    assert cell, "the occurred cell must show the short time and keep the stored value in its title"
    assert "rows.map(function (ev, i) {" in page, "the row index is needed to look at the row above"
    assert "<td>{ev.occurred_at}</td>" not in page
