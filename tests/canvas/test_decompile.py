"""The decompiler: canvas_get's program, and the event lines under it."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from primer.canvas.decompile import decompile, event_line
from primer.canvas.lang import parse_program
from primer.canvas.state import CanvasState, Changed, Limits, apply_batch
from tests.canvas._program_gen import UNPARSEABLE, program

GOLDEN = Path(__file__).parent / "golden"
NOW = datetime(2026, 10, 18, 12, 0, 12, tzinfo=timezone.utc)
AT = "2026-10-18T12:00:00+00:00"


def G(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


def built(text: str, state: CanvasState | None = None) -> CanvasState:
    result = apply_batch(state or CanvasState.empty(), parse_program(text), limits=Limits())
    assert result.errors == (), [e.render() for e in result.errors]
    return result.state


def _playlist_after_update() -> CanvasState:
    """The create and the update programs applied, a user having chosen Lo-fi and started the track."""
    state = built(G("playlist_update.cl"), built(G("playlist_create.cl")))
    state.rev = 7
    state.values = {"genre": "Lo-fi", "player": {"playing": True, "position": 42.0}}
    state.value_set_by = {"genre": "user", "player": "user"}
    return state


# --- the plan's tests ----------------------------------------------------------------------


def test_the_state_example_decompiles_to_the_golden():
    event = {"id": "e17", "element": "genre", "kind": "change", "value": "Lo-fi", "by": "alice", "at": AT}
    assert decompile(_playlist_after_update(), events=[event], now=NOW) == G("playlist_state.cl")


def test_full_round_trips_through_the_parser():
    s = _playlist_after_update()
    text = decompile(s, full=True)
    rebuilt = apply_batch(CanvasState.empty(), parse_program(text), limits=Limits()).state
    # The player's playing state is the user's and read-only: it is a comment, not a statement.
    assert "# user: playing at 0:42" in text
    assert rebuilt.elements == s.elements
    assert rebuilt.values == {k: v for k, v in s.values.items() if k != "player"}


def test_the_default_program_reapplied_changes_nothing():
    s = _playlist_after_update()
    result = apply_batch(s, parse_program(decompile(s)), limits=Limits())
    again = result.state
    assert (again.elements, again.values, again.value_set_by) == (s.elements, s.values, s.value_set_by)
    assert result.changed == Changed() and result.warnings == ()


def test_an_empty_canvas():
    assert decompile(CanvasState.empty()) == "# canvas rev 0: empty\n"
    cleared = built("clear", _playlist_after_update())
    assert decompile(cleared) == "# canvas rev 7: empty\n"


def test_ids_give_a_subtree():
    s = _playlist_after_update()
    assert decompile(s, ids=["controls"]) == (
        '# canvas rev 7; lines marked "# user" were set by the user\n'
        "# part of the canvas: controls\n"
        "controls = Row([genre, play, skip])\n"
        'genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")\n'
        'genre.value = "Lo-fi"  # user\n'
        'play = Button("Play", "primary", disabled=true)\n'
        'skip = Button("Skip")\n'
    )


def test_several_ids_a_nested_id_and_an_unknown_one():
    s = _playlist_after_update()
    text = decompile(s, ids=["title", "genre", "controls", "nope"])
    lines = text.splitlines()
    assert lines[1] == "# part of the canvas: title, genre, controls, nope"
    assert lines[2] == "# no element named 'nope'"
    names = [line.split(" = ")[0] for line in lines[3:] if " = " in line and "." not in line.split(" = ")[0]]
    assert names == ["title", "genre", "controls", "play", "skip"]       # genre is not printed a second time
    assert decompile(s, ids=[]) == decompile(s)


def test_event_lines():
    assert event_line({"id": "e1", "element": "play", "kind": "click", "by": "bob", "at": AT}, now=NOW) == 'e1 play click by="bob" 12s ago'
    emit = {"id": "e2", "element": "viz", "kind": "emit", "name": "queued", "data": {"n": 3, "tags": ["a", "b"]}, "by": "alice", "at": AT}
    assert event_line(emit, now=NOW) == 'e2 viz emit name="queued" data={"n":3,"tags":["a","b"]} by="alice" 12s ago'
    assert event_line({"id": "e3", "element": "viz", "kind": "emit", "name": "done", "by": "alice"}) == 'e3 viz emit name="done" by="alice"'
    assert event_line({"id": "e4", "element": "player", "kind": "play", "position": 42.0, "by": "alice", "at": AT}, now=NOW + timedelta(seconds=168)) == (
        'e4 player play position=42 by="alice" 3m ago'
    )
    assert event_line({"id": "e5", "element": "player", "kind": "pause", "position": 42.318}) == "e5 player pause position=42.3"
    assert event_line({"id": "e6", "element": "player", "kind": "ended", "position": 190, "by": "alice"}) == 'e6 player ended position=190 by="alice"'
    assert event_line({"id": "e7", "element": "viz", "kind": "set_value", "value": {"a": [1, None]}, "by": "al"}) == 'e7 viz set_value value={"a": [1, null]} by="al"'
    assert event_line({"id": "e8", "element": "tags", "kind": "change", "value": ["a", "b"]}) == 'e8 tags change value=["a", "b"]'
    assert event_line({"id": "e9", "element": "more", "kind": "open"}) == "e9 more open"


@pytest.mark.parametrize("position", [
    pytest.param(10**400, id="huge"), pytest.param(-(10**400), id="huge-negative"), pytest.param(10**5000, id="beyond-the-str-limit"),
    float("nan"), float("inf"), -float("inf"), True, "7",
])
def test_a_position_that_is_no_readable_number_prints_as_zero_and_never_crashes(position):
    # an integer beyond the range of a float used to raise OverflowError out of event_line
    assert event_line({"id": "e1", "element": "p", "kind": "play", "position": position}) == "e1 p play position=0"
    assert event_line({"id": "e2", "element": "p", "kind": "ended", "position": position, "by": "al"}) == 'e2 p ended position=0 by="al"'


def test_the_age_of_an_event():
    def ago(seconds: float, at: str | datetime = AT) -> str:
        return event_line({"id": "e", "element": "x", "kind": "click", "at": at}, now=datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds))

    assert [ago(s) for s in (0, 1, 59, 60, 119, 120, 3600, 7260)] == [
        "e x click 0s ago", "e x click 1s ago", "e x click 59s ago", "e x click 1m ago", "e x click 1m ago", "e x click 2m ago", "e x click 60m ago", "e x click 121m ago",
    ]
    assert ago(-5) == "e x click 0s ago"                                                  # a clock that is a little behind
    assert ago(12, "2026-10-18T12:00:00Z") == "e x click 12s ago"                          # a Z suffix
    assert ago(12, "2026-10-18T12:00:00") == "e x click 12s ago"                           # naive means UTC
    assert ago(12, datetime(2026, 10, 18, 12, 0, 0)) == "e x click 12s ago"
    assert event_line({"id": "e", "element": "x", "kind": "click", "at": "yesterday"}, now=NOW) == "e x click"      # unreadable: no age
    assert event_line({"id": "e", "element": "x", "kind": "click", "at": AT}) == "e x click"                      # no clock: no age


def test_more_than_twenty_events_are_counted():
    events = [{"id": f"e{i}", "element": "play", "kind": "click", "by": "alice"} for i in range(25)]
    text = decompile(_playlist_after_update(), events=events)
    tail = text.splitlines()[-22:]
    assert tail[0] == "# events since your last read (25):"
    assert tail[1] == '#   e0 play click by="alice"' and tail[20] == '#   e19 play click by="alice"'
    assert tail[21] == "#   ... and 5 more" and len(tail) == 22
    exactly = decompile(_playlist_after_update(), events=events[:20]).splitlines()
    assert exactly[-1] == '#   e19 play click by="alice"' and "more" not in exactly[-1]
    assert exactly[-21] == "# events since your last read (20):"


# --- how an element is written -------------------------------------------------------------------


def body(text: str) -> list[str]:
    """The lines of a one-element canvas (the element is 'a') after the header and the root line."""
    state = built(f"root = Column([a])\n{text}")
    return decompile(state).splitlines()[2:]


@pytest.mark.parametrize("source", [
    'a = Divider()',
    'a = Text("x")',
    'a = Text("x", "h1")',
    'a = Text("x", hidden=true)',
    'a = Text("x", "caption", hidden=true, disabled=true)',
    'a = Button("Go")',
    'a = Button("Go", "primary")',
    'a = Button("Go", disabled=true)',
    'a = Button("Go", "danger", disabled=true, wake=false)',
    'a = Button("Go", wake=false, hidden=true)',
    'a = Select("L", ["a", "b"])',
    'a = Select("L", ["a", "b"], "b")',
    'a = Select("L", ["a", "b"], multiple=true)',
    'a = Select("L", ["a", "b"], ["a"], multiple=true, wake=true)',
    'a = Select("L", [["x", "A"], ["y", "B"]], "y")',
    'a = Image("https://cdn.example.com/a.png", "alt")',
    'a = Image("https://cdn.example.com/a.png", "alt", height=100)',
    'a = Image("https://cdn.example.com/a.png", "alt", "cover", 100)',
    'a = Audio("https://cdn.example.com/a.mp3")',
    'a = Audio("https://cdn.example.com/a.mp3", "Title")',
    'a = Audio("https://cdn.example.com/a.mp3", loop=true)',
    'a = Audio("https://cdn.example.com/a.mp3", "Title", loop=true)',
    'a = Link("Open", "https://example.com/x?y=1")',
])
def test_each_element_is_written_in_the_shortest_form_and_reads_back(source):
    assert body(source) == [source]


def test_containers_write_their_children_first_then_the_rest():
    state = built(
        'root = Column([a, b, c, d], 16, "center")\n'
        'a = Row([], align="start", wrap=false)\n'
        'b = Card([e], "Title")\n'
        'c = Column([], align="end")\n'
        'd = Row([e2], 0)\n'
        'e = Divider()\n'
        'e2 = Divider()\n'
    )
    lines = decompile(state).splitlines()
    assert lines[1:] == [
        'root = Column([a, b, c, d], 16, "center")',
        'a = Row([], align="start", wrap=false)',
        'b = Card([e], "Title")',
        "e = Divider()",
        'c = Column([], align="end")',
        "d = Row([e2], 0)",
        "e2 = Divider()",
    ]


def test_strings_are_written_as_json_with_unicode_kept():
    source = 'a = Text("café \U0001F600 \\"quoted\\" \\\\ \\n tab\\t")'
    assert body(source) == [source]


def test_the_elided_html_counts_characters_not_bytes():
    state = built('root = Column([a])\na = Html("' + "é" * 10 + '")')
    assert decompile(state).splitlines()[2] == "a = Html(...)  # 10 chars; canvas_get full=true shows the body"
    assert body('a = Html("<b>x</b>", 80)') == ["a = Html(..., 80)  # 8 chars; canvas_get full=true shows the body"]


@pytest.mark.parametrize("html, literal", [
    ("<b>hi</b>", '"<b>hi</b>"'),
    ("<p>café</p>", '"<p>café</p>"'),
    ('<a href="x">go</a>', '"""<a href="x">go</a>"""'),
    ("line one\nline two", '"""line one\nline two"""'),
    ('say "hi"', '"""say "hi""""'),
    ('"quoted" start', '""""quoted" start"""'),
    ("back\\slash", '"""back\\slash"""'),
    ("tab\there", '"tab\\there"'),
    ('has """ inside\nand a newline', '"has \\"\\"\\" inside\\nand a newline"'),
    ("carriage\r\nreturn", '"carriage\\r\\nreturn"'),
    ("bell\x07 and\nnewline", '"bell\\u0007 and\\nnewline"'),
])
def test_a_full_html_body_is_written_verbatim_in_triple_quotes_when_that_is_safe(html, literal):
    state = CanvasState.empty()
    state.elements["root"]["children"] = ["a"]
    state.elements["a"] = {"type": "Html", "props": {"html": html}}
    text = decompile(state, full=True)
    assert text.splitlines()[2:] == (f"a = Html({literal})").splitlines()
    assert built(text).elements == state.elements


# --- values and the user's marks -------------------------------------------------------------------


def test_a_value_line_follows_its_element_and_is_marked_only_when_the_user_set_it():
    s = built('root = Column([a, b, c])\na = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")\nb = Html("<b>x</b>")\nc = Select("Tags", ["x", "y", "z"], multiple=true)')
    s.values = {"a": "Lo-fi", "b": {"picked": [1, 2]}, "c": ["x", "z"]}
    s.value_set_by = {"a": "user", "b": "agent", "c": "user"}
    lines = decompile(s).splitlines()[2:]
    assert lines == [
        'a = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")',
        'a.value = "Lo-fi"  # user',
        "b = Html(...)  # 8 chars; canvas_get full=true shows the body",
        'b.value = {"picked": [1, 2]}',
        'c = Select("Tags", ["x", "y", "z"], multiple=true)',
        'c.value = ["x", "z"]  # user',
    ]


def test_no_value_line_when_the_value_is_the_props_own():
    s = built('root = Column([a])\na = Select("Genre", ["Jazz", "Lo-fi"], "Jazz")')
    s.values, s.value_set_by = {"a": "Jazz"}, {"a": "user"}
    assert decompile(s).splitlines()[2:] == ['a = Select("Genre", ["Jazz", "Lo-fi"], "Jazz")']


@pytest.mark.parametrize("state_value, comment", [
    ({"playing": True, "position": 42.0}, "  # user: playing at 0:42"),
    ({"playing": False, "position": 185.4}, "  # user: paused at 3:05"),
    ({"playing": False, "position": 3723}, "  # user: paused at 62:03"),
    ({"playing": True}, "  # user: playing at 0:00"),
    ({"playing": True, "position": float("nan")}, "  # user: playing at 0:00"),
    ({"playing": True, "position": -4}, "  # user: playing at 0:00"),
    pytest.param({"playing": True, "position": 10**400}, "  # user: playing at 0:00", id="huge"),
    pytest.param({"playing": False, "position": -(10**400)}, "  # user: paused at 0:00", id="huge-negative"),
    ({"playing": False, "position": 190.0, "ended": True}, "  # user: ended"),
    ({"ended": True}, "  # user: ended"),
    ({}, ""),
    ({"volume": 3}, ""),
    ("playing", ""),
    (None, ""),
])
def test_audio_state_is_a_comment(state_value, comment):
    s = built('root = Column([p])\np = Audio("https://cdn.example.com/a.mp3", "T")')
    if state_value is not None:
        s.values, s.value_set_by = {"p": state_value}, {"p": "user"}
    assert decompile(s).splitlines()[2] == 'p = Audio("https://cdn.example.com/a.mp3", "T")' + comment


# --- properties ---------------------------------------------------------------------------------------


def _current_values(state: CanvasState) -> dict[str, object]:
    return {eid: state.current_value(eid) for eid in state.elements if state.current_value(eid) is not None}


def test_decompiled_programs_of_generated_canvases_parse_and_rebuild_the_same_canvas():
    checked = 0
    for seed in range(120):
        r = random.Random(seed)
        state = CanvasState.empty()
        for _ in range(6):
            text = program(r, state)
            if UNPARSEABLE in text:
                continue
            result = apply_batch(state, parse_program(text), limits=Limits(max_elements=15))
            if result.state is None:
                continue
            state = result.state
            for eid, element in state.elements.items():
                if element["type"] == "Select" and r.random() < 0.3:
                    choices = [o if isinstance(o, str) else o[0] for o in element["props"]["options"]]
                    state.values[eid] = [choices[-1]] if element["props"].get("multiple") else choices[-1]
                    state.value_set_by[eid] = "user"
            # a full program recreates the canvas from nothing
            rebuilt = apply_batch(CanvasState.empty(), parse_program(decompile(state, full=True)), limits=Limits(max_elements=15))
            assert rebuilt.errors == (), ([e.render() for e in rebuilt.errors], decompile(state, full=True))
            assert rebuilt.state.elements == state.elements
            assert _current_values(rebuilt.state) == _current_values(state)
            # the default program, sent back to the same canvas, changes nothing
            again = apply_batch(state, parse_program(decompile(state)), limits=Limits(max_elements=15))
            assert again.errors == (), ([e.render() for e in again.errors], decompile(state))
            assert again.state.elements == state.elements and again.changed == Changed()
            assert _current_values(again.state) == _current_values(state)
            checked += 1
    assert checked > 300
