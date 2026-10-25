"""Canvas chips in the transcript (pivot P5, Task 22): a canvas click, change or
operator edit reads as a slim chip, not a raw record or a generic lifecycle dot.

The pure seams are in session-adapter.jsx (SA_toTranscript's mapping plus
SA_canvasChipLabel / SA_canvasDigestLine) and shell-turns.js (the canvas tool
verbs plus SH_toolChipLabel's canvas_apply result).

What the user ENTERED never reaches the label. The S5 strip (the privacy ruling
of #872) removes a ``canvas.event``'s ``value`` and ``data`` from every
structured copy the transcript reads - the ``canvas_event`` record, the
``user_input``'s ``canvas_event`` attribution and the digest's ``events`` - and
the transcript is a shared read, so a user's input must not appear in it. A
change therefore names only the control: ``alice changed Genre``, never
``to "Lo-fi"`` (the lead's ruling after S9 dispatch).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "ui" / "components" / "session-adapter.jsx"
TURNS = ROOT / "ui" / "foundation" / "shell-turns.js"
STATUS = ROOT / "ui" / "foundation" / "shell-status.js"
NV_SESSION_DOC = ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx"

AT = "2026-10-25T12:00:00Z"


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def ctx():
    py_mini_racer = pytest.importorskip("py_mini_racer")
    # session-adapter.jsx is pure JS (no JSX in it: its two <Transcript>
    # mentions are comments), so it evals directly, the same way
    # test_shell_turns.py's harness loads it. shell-turns.js needs
    # SH_bareToolName (shell-status.js) and the adapter's result readers.
    c = py_mini_racer.MiniRacer()
    c.eval("var window = globalThis;")
    c.eval(STATUS.read_text(encoding="utf-8"))
    c.eval(ADAPTER.read_text(encoding="utf-8"))
    c.eval(TURNS.read_text(encoding="utf-8"))
    try:
        yield c
    finally:
        c.close()


# --- record fixtures (the exact shapes the writers produce) -------------------


def canvas_event_rec(seq: int, *, element, etype, kind, label=None, name=None,
                     position=None, by="alice", wake=False) -> dict:
    """A ``canvas_event`` record: the event WITHOUT its input (value/data stripped)."""
    payload = {
        "id": "e" + str(seq), "seq": seq, "rev": 2, "element": element, "type": etype,
        "kind": kind, "source": "control", "by": by, "wake": wake, "at": AT,
    }
    if label is not None:
        payload["label"] = label
    if name is not None:
        payload["name"] = name
    if position is not None:
        payload["position"] = position
    return {"seq": seq, "kind": "canvas_event", "payload": payload}


def woken_user_input(seq: int, attribution: dict, text: str = "<canvas_event ...>") -> dict:
    """A ``user_input`` whose payload carries the click's attribution: the block
    text is the session's own input; the reader sees only the chip label."""
    return {"seq": seq, "kind": "user_input",
            "payload": {"text": text, "canvas_event": attribution}}


def canvas_op_rec(seq: int, *, by, changed, ops, call_id=None) -> dict:
    """A ``canvas_op`` record: the batch's change summary and ops (+ call_id for
    an agent batch, whose tool rows already show it)."""
    payload = {"rev": seq, "source": "agent" if call_id else "operator", "by": by,
               "changed": changed, "ops": ops}
    if call_id is not None:
        payload["call_id"] = call_id
    return {"seq": seq, "kind": "canvas_op", "payload": payload}


CHANGED_23_0 = {"added": ["a", "b"], "updated": ["c", "d", "e"], "removed": []}
CLEAR_OPS = [{"op": "clear"}]
SET_OPS = [{"op": "set", "id": "a", "type": "Button", "props": {}}]


# --- the click, as a record and as a woken wake -------------------------------


def test_a_click_record_and_a_woken_click_render_as_chips(ctx) -> None:
    """A recorded click (a ``canvas_event`` a wait took) and a woken click (a
    ``user_input`` carrying the attribution) both read as a chip - never a raw
    record and never the generic lifecycle bubble."""
    rec = canvas_event_rec(1, element="play", etype="Button", kind="click",
                           label="Play", by="alice", wake=True)
    attr = {"id": "e1", "seq": 1, "element": "play", "type": "Button",
            "kind": "click", "label": "Play", "by": "alice"}
    woken = woken_user_input(2, attr)
    rows = js(ctx, "SA_toTranscript(" + json.dumps([rec, woken]) + ", {})")
    chips = [r for r in rows if r["kind"] == "canvas_chip"]
    assert [c["label"] for c in chips] == ["alice clicked Play", "alice clicked Play"]
    # a click never falls through to the generic lifecycle bubble
    assert not [r for r in rows if r["kind"] == "lifecycle"]


def test_a_change_names_only_the_control(ctx) -> None:
    """The privacy ruling: a change chip names the control (the element's
    label), never the value the user entered - it is stripped from the record
    and the transcript is a shared read."""
    rec = canvas_event_rec(1, element="genre", etype="Select", kind="change",
                           label="Genre", by="alice")
    assert js(ctx, "SA_canvasChipLabel(" + json.dumps(rec) + ")") == "alice changed Genre"


# --- an agent batch shows once, via its tool chip -----------------------------


def test_an_agent_batch_is_shown_once_by_its_tool_chip(ctx) -> None:
    """A ``canvas_op`` that carries a ``call_id`` is the agent's own batch: its
    tool_call + tool_result already show it (the tool chip), so the record is
    skipped. The tool chip itself carries the change counts."""
    op = canvas_op_rec(10, by="agent", changed=CHANGED_23_0, ops=SET_OPS,
                       call_id="call-1")
    call = {"seq": 11, "kind": "tool_call",
            "payload": {"name": "canvas__canvas_apply",
                        "arguments": {"source": "root = Column([a])"}}}
    result = {"seq": 12, "kind": "tool_result",
              "payload": {"call_id": "call-1", "is_error": False,
                          "output": json.dumps({"rev": 5, "changed": CHANGED_23_0,
                                                "warnings": []})}}
    rows = js(ctx, "SA_toTranscript(" + json.dumps([op, call, result]) + ", {})")
    assert [r["kind"] for r in rows if r["kind"] == "canvas_chip"] == []
    assert js(ctx, "SH_toolChipLabel(" + json.dumps(call) + ", " + json.dumps(result)
              + ").label") == "updated the canvas +2 ~3 -0"


def test_an_operator_batch_is_a_chip(ctx) -> None:
    """An operator batch (no tool rows) reads as its own chip with the change
    counts; a clear reads as a clear."""
    op = canvas_op_rec(11, by="alice", changed=CHANGED_23_0, ops=SET_OPS)
    rows = js(ctx, "SA_toTranscript(" + json.dumps([op]) + ", {})")
    chips = [r for r in rows if r["kind"] == "canvas_chip"]
    assert len(chips) == 1 and chips[0]["label"] == "alice updated the canvas +2 ~3 -0"

    cleared = canvas_op_rec(12, by="alice",
                            changed={"added": [], "updated": [], "removed": ["a", "b"]},
                            ops=CLEAR_OPS)
    assert js(ctx, "SA_canvasChipLabel(" + json.dumps(cleared) + ")") == "alice cleared the canvas"


# --- the other interaction kinds ----------------------------------------------


@pytest.mark.parametrize("element, etype, kind, label, name, expected", [
    ("viz", "Html", "emit", None, "pick", "alice sent pick from viz"),
    ("viz", "Html", "set_value", None, None, "alice set viz"),
    ("player", "Audio", "play", "So What", None, "alice played So What"),
    ("player", "Audio", "pause", "So What", None, "alice paused So What"),
    ("player", "Audio", "ended", "So What", None, "alice finished So What"),
    ("link", "Link", "open", "Open on Bandcamp", None, "alice opened Open on Bandcamp"),
])
def test_the_other_kinds_read_as_chips(ctx, element, etype, kind, label, name, expected) -> None:
    rec = canvas_event_rec(1, element=element, etype=etype, kind=kind,
                           label=label, name=name, by="alice")
    assert js(ctx, "SA_canvasChipLabel(" + json.dumps(rec) + ")") == expected


def test_an_unlabelled_control_falls_back_to_its_id(ctx) -> None:
    """A click on an element with no label names the element id, not an empty
    control."""
    rec = canvas_event_rec(1, element="go", etype="Button", kind="click",
                           label=None, by="alice", wake=True)
    assert js(ctx, "SA_canvasChipLabel(" + json.dumps(rec) + ")") == "alice clicked go"


# --- the digest line ------------------------------------------------------------


def test_a_digest_line(ctx) -> None:
    """A change item is the control's label alone (no value); an audio event
    carries its past-tense verb. The line joins the listed items."""
    digest = {"rev": 2, "events": [
        {"id": "e1", "seq": 1, "element": "genre", "type": "Select", "kind": "change",
         "label": "Genre", "by": "alice"},
        {"id": "e2", "seq": 2, "element": "player", "type": "Audio", "kind": "play",
         "label": "So What", "by": "alice"},
    ]}
    assert js(ctx, "SA_canvasDigestLine(" + json.dumps(digest) + ")") == \
        'canvas changes: Genre, So What played'


def test_a_digest_line_counts_the_ones_it_leaves_out(ctx) -> None:
    """At most three items are shown; the rest are counted (and stay unread,
    read with canvas_get)."""
    events = [
        {"id": "e1", "seq": 1, "element": "genre", "type": "Select", "kind": "change", "label": "Genre"},
        {"id": "e2", "seq": 2, "element": "player", "type": "Audio", "kind": "play", "label": "So What"},
        {"id": "e3", "seq": 3, "element": "player", "type": "Audio", "kind": "pause", "label": "So What"},
        {"id": "e4", "seq": 4, "element": "mood", "type": "Select", "kind": "change", "label": "Mood"},
        {"id": "e5", "seq": 5, "element": "link", "type": "Link", "kind": "open", "label": "My Link"},
    ]
    assert js(ctx, "SA_canvasDigestLine(" + json.dumps({"rev": 3, "events": events}) + ")") == \
        "canvas changes: Genre, So What played, So What paused, and 2 more"


def test_an_empty_digest_says_so(ctx) -> None:
    assert js(ctx, "SA_canvasDigestLine(" + json.dumps({"rev": 1, "events": []}) + ")") == \
        "canvas changes: (none)"


# --- the queued row reads its click as a chip ----------------------------------


def test_a_queued_click_shows_its_label() -> None:
    """A queued steer whose attribution carries a canvas click shows the chip
    label, not its raw block text: the block (the session's own input) holds
    what the user entered and must not be drawn for the shared reader."""
    src = NV_SESSION_DOC.read_text(encoding="utf-8")
    # the queued-row map (nv-queued) reads attribution.canvas_event and renders
    # the chip label through SA_canvasChipLabel when it is set
    assert "nv-queued" in src
    assert "canvas_event" in src
    # the queued row resolves its label through the adapter, not the raw parts text
    i = src.index("nv-queued")
    block = src[max(0, i - 1200): i + 400]
    assert "SA_canvasChipLabel" in block
    assert "attribution" in block
