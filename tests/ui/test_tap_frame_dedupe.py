"""Lane I2 carry: the shared tap hub dedupes frames by (session_id, seq).

A reconnect replays frames past the resume cursor. A replayed frame must not push a unique
event out of the hub's rolling 500-event buffer, and the per-frame listeners must not fire
for it a second time.

MiniRacer over the real hub with a fake ``EventSource`` (same pattern as
``test_workspace_tap_reconnect``). Not run in a browser.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from tests.ui._mini_react import ROOT

TAP_SRC = (ROOT / "ui" / "foundation" / "use-workspace-tap.js").read_text(encoding="utf-8")

PRELUDE = r"""
var window = globalThis;
var OPENED = [];
function EventSource(url) { this.url = url; this.readyState = 0; this.closed = false; OPENED.push(this); }
EventSource.prototype.close = function () { this.closed = true; this.readyState = 2; };
function last() { return OPENED[OPENED.length - 1]; }
"""


@pytest.fixture
def ctx() -> Iterator[object]:
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval(PRELUDE)
    c.eval(TAP_SRC)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify((function () { return " + expr + "; })())"))


def open_hub(ctx, wid: str = "w1") -> None:
    ctx.eval("WTAP_retain('" + wid + "');")
    ctx.eval("var SEEN = [];")
    ctx.eval(
        "WTAP_HUBS['" + wid + "'].eventSubs.push(function (ev) { "
        "SEEN.push(ev.session_id + ':' + ev.seq); });"
    )
    ctx.eval("last().onopen();")


def deliver(ctx, sid: str, seq: int) -> None:
    ctx.eval(
        "last().onmessage({ data: JSON.stringify("
        + json.dumps({"session_id": sid, "seq": seq, "type": "session.tick"})
        + ") });"
    )


def test_a_replayed_frame_is_not_buffered_and_not_delivered(ctx) -> None:
    open_hub(ctx)
    deliver(ctx, "a", 1)
    deliver(ctx, "a", 2)
    deliver(ctx, "a", 2)  # the reconnect replayed the same (session_id, seq)
    deliver(ctx, "a", 3)
    assert js(ctx, "SEEN") == ["a:1", "a:2", "a:3"], "listeners saw a duplicate"
    assert js(ctx, "WTAP_HUBS['w1'].events.length") == 3


def test_a_replay_cannot_push_a_unique_event_out_of_the_buffer(ctx) -> None:
    open_hub(ctx)
    for i in range(500):
        deliver(ctx, "s", i)
    deliver(ctx, "s", 0)  # replay of the oldest frame still in the replay window
    deliver(ctx, "s", 500)
    deliver(ctx, "s", 501)
    seqs = js(ctx, "WTAP_HUBS['w1'].events.map(function (e) { return e.seq; })")
    # Without the dedupe the replayed frame appends a 501st entry and pushes
    # unique frames out a tick early; with it the buffer holds exactly the
    # last 500 unique seqs.
    assert len(seqs) == 500
    assert seqs == list(range(2, 502)), seqs[:5]
    assert js(ctx, "SEEN.filter(function (k) { return k === 's:0'; }).length") == 1


def test_a_frame_without_both_key_fields_is_not_keyed(ctx) -> None:
    open_hub(ctx)
    ctx.eval("last().onmessage({ data: JSON.stringify({type: 'workspace.tick'}) });")
    ctx.eval("last().onmessage({ data: JSON.stringify({type: 'workspace.tick'}) });")
    assert js(ctx, "WTAP_HUBS['w1'].events.length") == 2
    assert js(ctx, "SEEN.length") == 2
