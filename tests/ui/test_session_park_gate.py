"""Park is offered only where it can do something, from every menu, and the header's rename failure is a toast (PR 501 review).

Parking a paused session was a no-op that toasted "paused" and left a stale ``pause_requested`` flag on the row, and the rail's menu
offered it where the overflow and the palette did not. One predicate, ``NV_canPark``, now gates the rail row, the overflow row and the
palette verb. The header's own click-to-rename also swallowed a failed rename into a plain toast: it goes through the shared failure
toast like every other session action.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
RAIL = (ROOT / "ui" / "components" / "console" / "nv-rail.jsx").read_text(encoding="utf-8")


@pytest.fixture
def can_park():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    start = DOC.index("function NV_canPark")
    ctx.eval(DOC[start:DOC.index("\n}\n", start) + len("\n}\n")])
    try:
        yield lambda session: ctx.eval("NV_canPark(" + json.dumps(session) + ")")
    finally:
        ctx.close()


def test_only_a_live_session_that_is_not_already_paused_can_be_parked(can_park) -> None:
    assert can_park({"status": "running"}) and can_park({"status": "waiting"}) and can_park({"status": "created"})
    assert not can_park({"status": "paused"})
    assert not can_park({"status": "ended"})
    assert can_park(None), "a session the menu has not loaded yet keeps the row, as before"


def test_every_surface_gates_park_on_the_same_predicate() -> None:
    menu = RAIL[RAIL.index("function NV_Rail_SessionContextMenu"):]
    menu = menu[:menu.index("\n}\n")]
    assert menu.index("window.NV_canPark(s)") < menu.index('rows.push(act("Park"'), "the rail row is gated"
    assert "NV_canPark(session) ? (" in DOC[DOC.index("function NV_SessionHeader"):], "and so is the overflow row"
    verb = DOC[DOC.index('id: "session.park"'):]
    assert "available: function (ctx) { return NV_canPark(ctx.session); }" in verb[:500], "and so is the palette verb"
    assert "window.NV_canPark = NV_canPark;" in DOC


def test_the_headers_own_rename_failure_goes_through_the_shared_toast() -> None:
    header = DOC[DOC.index("function NV_SessionHeader"):DOC.index("function NV_Thought")]
    save = header[header.index("function saveTitle()"):]
    save = save[:save.index("\n  }\n")]
    assert 'NV_failToast(con.toast, "Rename", err)' in save
    assert "Rename failed" not in save
