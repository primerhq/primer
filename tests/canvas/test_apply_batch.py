"""The canvas state and the atomic batch: apply_batch is all or nothing and names every bad line."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from primer.canvas.lang import SetProp, parse_program
from primer.canvas.state import (
    ROOT_ELEMENT,
    BatchResult,
    CanvasState,
    Changed,
    Limits,
    apply_batch,
    check_json_value,
)
from tests.canvas._program_gen import nested

GOLDEN = Path(__file__).parent / "golden"
S = CanvasState.empty()
L = Limits()
SELECT_SIG = "Select(label, options, value?, multiple?, wake?)"
TYPES = "Column, Row, Card, Divider, Text, Button, Select, Image, Audio, Link, Html"


def G(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


def run(text: str, state: CanvasState = S, **kw) -> BatchResult:
    kw.setdefault("limits", L)
    return apply_batch(state, parse_program(text), **kw)


def errs(text: str, state: CanvasState = S, **kw) -> list[str]:
    result = run(text, state, **kw)
    assert result.state is None, "expected the batch to be refused"
    return [e.render() for e in result.errors]


def _built() -> CanvasState:
    """The playlist, with the user's choice of Lo-fi on the Genre select."""
    state = run(G("playlist_create.cl")).state
    state.values["genre"] = "Lo-fi"
    state.value_set_by["genre"] = "user"
    state.rev = 3
    return state


def _n_texts(n: int) -> str:
    ids = [f"t{i}" for i in range(n)]
    return "root = Column([" + ", ".join(ids) + "])\n" + "\n".join(f'{i} = Text("x")' for i in ids)


# --- the state ---------------------------------------------------------------------


def test_an_empty_state_has_only_the_root_column():
    s = CanvasState.empty()
    assert (s.rev, s.elements, s.values, s.value_set_by) == (0, {"root": ROOT_ELEMENT}, {}, {})
    assert ROOT_ELEMENT == {"type": "Column", "props": {}, "children": []}
    assert s.elements["root"] is not ROOT_ELEMENT
    assert s.element_count() == 0


def test_the_state_round_trips_through_json_and_copies_are_independent():
    s = _built()
    again = CanvasState.from_json(json.loads(json.dumps(s.to_json())))
    assert again == s
    clone = s.copy()
    clone.elements["title"]["props"]["text"] = "changed"
    clone.values["genre"] = "Jazz"
    clone.value_set_by["genre"] = "agent"
    assert s.elements["title"]["props"]["text"] == "Pick a playlist"
    assert (s.values["genre"], s.value_set_by["genre"]) == ("Lo-fi", "user")
    assert s.to_json()["elements"] is not s.elements


def test_from_json_tolerates_a_sparse_document():
    s = CanvasState.from_json({"rev": 4})
    assert s == CanvasState(4, {"root": ROOT_ELEMENT}, {}, {})
    assert CanvasState.from_json({}).rev == 0
    assert "root" in CanvasState.from_json({"elements": {"a": {"type": "Divider", "props": {}}}}).elements


def test_current_value_prefers_the_value_over_the_prop():
    s = _built()
    assert s.current_value("genre") == "Lo-fi"
    del s.values["genre"]
    assert s.current_value("genre") == "Jazz"
    assert s.current_value("title") is None and s.current_value("nope") is None


def test_element_count_excludes_the_root_and_size_is_compact_utf8_json():
    s = _built()
    assert s.element_count() == 8
    expected = len(json.dumps({"elements": s.elements, "values": s.values}, separators=(",", ":"), ensure_ascii=False).encode())
    assert s.size_bytes() == expected
    s.elements["title"]["props"]["text"] = "é" * 10
    assert s.size_bytes() == len(json.dumps({"elements": s.elements, "values": s.values}, separators=(",", ":"), ensure_ascii=False).encode())


def test_changed_summary():
    assert Changed(("a", "b"), ("c",), ()).summary() == "+2 ~1 -0"
    assert Changed().summary() == "+0 ~0 -0"


# --- the plan's tests ----------------------------------------------------------------


def test_the_playlist_builds_the_stored_form():
    r = apply_batch(S, parse_program(G("playlist_create.cl")), limits=L)
    assert r.errors == () and r.changed.updated == ("root",)
    assert r.changed.added == ("title", "cover", "controls", "genre", "play", "player", "more", "viz")
    assert r.state.elements["genre"] == {"type": "Select", "props": {"label": "Genre", "options": ["Jazz", "Lo-fi", "Classical"], "value": "Jazz"}}
    assert r.state.elements["controls"] == {"type": "Row", "props": {}, "children": ["genre", "play"]}
    assert r.state.element_count() == 8 and S.element_count() == 0
    assert r.changed.summary() == "+8 ~1 -0"
    assert r.warnings == ()


def test_the_stored_form_leaves_out_defaults_and_non_containers_have_no_children_key():
    r = run('root = Column([a, b, c])\na = Text("x", "body")\nb = Button("Go", "secondary", false, true)\nc = Row([], 8, "center", true)')
    assert r.state.elements["a"] == {"type": "Text", "props": {"text": "x"}}
    assert r.state.elements["b"] == {"type": "Button", "props": {"label": "Go"}}
    assert r.state.elements["c"] == {"type": "Row", "props": {}, "children": []}
    assert r.state.elements["root"] == {"type": "Column", "props": {}, "children": ["a", "b", "c"]}


def test_a_batch_with_bad_lines_applies_nothing_and_names_every_bad_line():          # Review Focus 3
    r = apply_batch(S, parse_program('a = Text("x")\nb = Select("Genre", "Jazz")\nc = Buton("x")\nroot += [a, b, c]'), limits=L)
    assert r.state is None
    assert [e.render() for e in r.errors] == [
        "line 2: Select needs options as a list (Select(label, options, value?, multiple?, wake?))",
        "line 3: unknown element type 'Buton' (Column, Row, Card, Divider, Text, Button, Select, Image, Audio, Link, Html)",
    ]
    assert r.changed == Changed() and r.warnings == ()


def test_replacing_an_element_keeps_a_still_valid_user_value():          # Review Focus 2
    s = _built()
    r = run('genre = Select("Genre", ["Jazz", "Lo-fi"])', s)
    kept = r.state
    assert kept.values == {"genre": "Lo-fi"} and kept.value_set_by == {"genre": "user"}
    assert r.warnings == ()
    r = run('genre = Select("Genre", ["Jazz"])', s)
    gone = r.state
    assert "genre" not in gone.values and "genre" not in gone.value_set_by
    assert r.warnings == ("genre: the user's value was reset because the element changed",)
    retyped = run('genre = Text("x")', s).state
    assert "genre" not in retyped.values


def test_setting_a_value_to_itself_keeps_who_set_it():                   # Review Focus 2
    r = run('genre.value = "Lo-fi"', _built())
    assert r.state.value_set_by == {"genre": "user"}
    assert r.changed == Changed()


def test_an_agent_value_marks_the_agent_and_changes_the_element():
    r = run('genre.value = "Classical"', _built())
    assert r.state.values == {"genre": "Classical"} and r.state.value_set_by == {"genre": "agent"}
    assert r.changed == Changed((), ("genre",), ())
    # the stored prop is the initial value; the live value sits beside it
    assert r.state.elements["genre"]["props"]["value"] == "Jazz"
    assert r.state.current_value("genre") == "Classical"


def test_keep_reuses_the_current_argument():
    s = run(G("playlist_create.cl")).state
    r = run("viz = Html(..., 80)", s)
    assert r.state.elements["viz"]["props"] == {"html": s.elements["viz"]["props"]["html"], "height": 80}
    assert errs("viz = Html(..., 80)") == ["line 1: ... keeps the current value, but 'viz' does not exist yet"]
    assert errs('title = Html(..., 80)', s) == ["line 1: ... keeps the current value, but 'title' is of type Text, not Html"]
    # keep on a property value changes nothing but still checks the element exists
    assert run("viz.height = ...", s).changed == Changed()
    assert errs("nope.height = ...", s) == ["line 1: no element named 'nope'"]
    # keep also covers a container's children and an omitted optional argument
    kept = run("controls = Row(..., 4)", s).state
    assert kept.elements["controls"] == {"type": "Row", "props": {"gap": 4}, "children": ["genre", "play"]}
    assert run("viz = Html(..., ...)", s).changed == Changed()             # height 60 is kept
    assert run("cover = Image(..., ..., ...)", s).changed == Changed()      # fit was never set, and stays unset


def test_null_removes_a_property():
    s = run("play.disabled = true", run(G("playlist_create.cl")).state).state
    assert s.elements["play"]["props"]["disabled"] is True
    r = run("play.disabled = null", s)
    assert "disabled" not in r.state.elements["play"]["props"]
    assert run("play.disabled = false", s).state.elements["play"]["props"] == {"label": "Play", "style": "primary"}
    assert errs("play.label = null", s) == ["line 1: Button needs label (Button(label, style?, disabled?, wake?))"]


def test_del_removes_the_subtree_and_the_parent_reference():
    s = _built()
    r = run("del controls", s)
    assert set(r.state.elements) == {"root", "title", "cover", "player", "more", "viz"}
    assert r.state.elements["root"]["children"] == ["title", "cover", "player", "more", "viz"]
    assert r.state.values == {} and r.state.value_set_by == {}
    assert r.changed == Changed((), ("root",), ("controls", "genre", "play"))
    assert run("del genre", s).state.elements["controls"]["children"] == ["play"]
    assert errs("del nope", s) == ["line 1: no element named 'nope'"]
    assert errs("del root", s) == ["line 1: root cannot be deleted; use clear to empty the canvas"]
    assert errs("del controls, genre", s) == ["line 1: no element named 'genre'"]


def test_clear_leaves_an_empty_root():
    r = run("clear", _built())
    assert r.state.elements == {"root": ROOT_ELEMENT} and r.state.values == {} and r.state.value_set_by == {}
    assert len(r.changed.removed) == 8 and r.changed.updated == ("root",) and r.changed.added == ()
    assert r.state.rev == 3
    again = run('clear\nroot = Column([a])\na = Text("x")', _built()).state
    assert set(again.elements) == {"root", "a"}


def test_root_is_always_a_column():
    assert errs('root = Text("x")') == ["line 1: root is always a Column"]
    r = run('root = Column([a], 16)\na = Text("x")')
    assert r.state.elements["root"] == {"type": "Column", "props": {"gap": 16}, "children": ["a"]}


def test_reachability_one_parent_and_cycles():
    assert errs('a = Text("x")') == ["line 1: 'a' is not reachable from root; add it to a container's children"]
    # one parent each: the second claim is reported on the line that made it
    assert errs('root = Column([col1, col2])\ncol1 = Column([a])\ncol2 = Column([a])\na = Text("x")') == [
        "line 3: 'a' is already a child of 'col1'"
    ]
    assert errs('root = Column([a, a])\na = Text("x")') == ["line 1: 'a' is already a child of 'root'"]
    # cycles: only the closing edge is reported, not a second parent on top of it
    assert errs("col = Column([col])\nroot = Column([col])") == ["line 1: 'col' would contain itself"]
    assert errs("root = Column([a])\na = Column([b])\nb = Column([a])") == ["line 3: 'a' would contain itself"]
    assert errs("root += [root]") == ["line 1: 'root' would contain itself"]
    assert errs("x = Column([root])\nroot = Column([x])") == ["line 1: 'root' would contain itself"]
    assert errs("x = Column([root])") == [
        "line 1: 'root' cannot be inside another element",
        "line 1: 'x' is not reachable from root; add it to a container's children",
    ]


def test_only_the_top_of_an_orphaned_subtree_is_reported_and_on_the_line_that_orphaned_it():
    s = _built()
    # re-listing the root's children without 'controls' orphans controls, genre and play on line 1
    assert errs("root = Column([title, cover, player, more, viz])", s) == [
        "line 1: 'controls' is not reachable from root; add it to a container's children"
    ]
    # replacing a container with fewer children orphans the dropped ones on that line
    assert errs('title = Text("t")\ncontrols = Row([genre])', s) == [
        "line 2: 'play' is not reachable from root; add it to a container's children"
    ]
    # deleting them in the same batch is the way out
    assert run("controls = Row([genre])\ndel play", s).errors == ()
    # and an orphan can be re-attached later in the batch
    assert run("controls = Row([genre])\nroot += [play]", s).errors == ()


def test_forward_references_are_allowed():
    r = run('root = Column([a])\na = Text("x")')
    assert r.errors == () and r.state.elements["root"]["children"] == ["a"]
    assert run('root += [a]\na = Text("x")').errors == ()


def test_an_unknown_child_is_reported_where_the_children_were_set():
    assert errs('root = Column([a, ghost])\na = Text("x")') == ["line 1: unknown element 'ghost' in the children of 'root'"]
    assert errs("root += [ghost]") == ["line 1: unknown element 'ghost' in the children of 'root'"]
    # a delete also removes the reference, so nothing dangles
    assert run("del title", _built()).errors == ()
    # but a later container that still names a deleted element does
    assert errs("del genre\nextra = Row([genre])\nroot += [extra]", _built()) == [
        "line 2: unknown element 'genre' in the children of 'extra'"
    ]


@pytest.mark.parametrize("program, message", [
    (_n_texts(201), "line 202: the batch would make 201 elements; the limit is 200"),
    ('root = Column([v])\nv = Html("' + "x" * 65537 + '")', "line 2: v: Html is 65537 bytes; the limit is 65536"),
])
def test_limits(program, message):
    assert errs(program) == [message]
    assert run(_n_texts(200)).errors == ()
    assert run('root = Column([v])\nv = Html("' + "x" * 65536 + '")').errors == ()


def test_the_canvas_size_limit():
    program = "root = Column([" + ", ".join(f"t{i}" for i in range(30)) + "])\n" + "\n".join(f't{i} = Text("{"y" * 100}")' for i in range(30))
    size = run(program, limits=Limits(max_canvas_bytes=10**9)).state.size_bytes()
    assert size > 3000
    assert errs(program, limits=Limits(max_canvas_bytes=2000)) == [f"line 31: the canvas would be {size} bytes; the limit is 2000"]
    assert run(program, limits=Limits(max_canvas_bytes=size)).errors == ()


def test_limits_are_operator_tunable_and_only_stop_growth():
    tight = Limits(max_elements=5)
    assert errs(_n_texts(6), limits=tight) == ["line 7: the batch would make 6 elements; the limit is 5"]
    assert run(_n_texts(5), limits=tight).errors == ()
    big = run(_n_texts(10)).state
    assert run("del t0", big, limits=tight).errors == ()                      # shrinking an over-limit canvas is allowed
    assert errs('root += [extra]\nextra = Text("x")', big, limits=tight) == ["line 2: the batch would make 11 elements; the limit is 5"]
    assert errs('root = Column([v])\nv = Html("12345")', limits=Limits(max_html_bytes=4)) == ["line 2: v: Html is 5 bytes; the limit is 4"]


def test_media_sources_and_links():
    ok = 'root = Column([i, p, l])\ni = Image("https://cdn.example.com/a.png", "A")\np = Audio("file:audio/a.mp3")\nl = Link("Go", "https://example.com")'
    assert run(ok).errors == ()
    base = 'root = Column([m])\n'
    sig = "Image(src, alt, fit?, height?)"
    use_https = "use https (or file:<path> for a workspace file)"
    assert errs(base + 'm = Image("http://cdn.example.com/a.png", "A")') == [f"line 2: {use_https} ({sig})"]
    assert errs(base + 'm = Image("file:../x.png", "A")') == [f"line 2: file: paths are relative to the workspace and stay inside it ({sig})"]
    assert errs(base + 'm = Image("file:.state/x.png", "A")') == [f"line 2: file: paths are relative to the workspace and stay inside it ({sig})"]
    assert errs(base + 'm = Image("file:a.svg", "A")') == [f"line 2: file:<path> must name a png, jpg, jpeg, gif, webp or avif image ({sig})"]
    assert errs(base + 'm = Image("javascript:alert(1)", "A")') == [f"line 2: {use_https} ({sig})"]
    assert errs(base + 'm = Link("Go", "javascript:alert(1)")') == ["line 2: Link href must be an http or https URL (Link(label, href))"]
    assert errs(base + 'm = Link("Go", "ftp://example.com")') == ["line 2: Link href must be an http or https URL (Link(label, href))"]
    assert errs(base + 'm = Audio("file:a.png")') == [
        "line 2: file:<path> must name an mp3, ogg, oga, wav, m4a, aac, flac or weba audio file (Audio(src, title?, loop?))"
    ]


def test_select_value_must_be_an_option():
    s = _built()
    assert errs('genre.value = "Rock"', s) == ["line 1: 'Rock' is not one of the options of 'genre'"]
    assert errs('root = Column([g])\ng = Select("Genre", ["Jazz"], "Rock")') == ["line 2: 'Rock' is not one of the options of 'g'"]
    assert errs('genre.value = ["Jazz"]', s) == ["line 1: 'genre' takes a single option, not a list"]
    multi = run('root = Column([g])\ng = Select("Genre", ["Jazz", "Lo-fi", "Classical"], multiple=true)').state
    assert errs('g.value = "Jazz"', multi) == ["line 1: 'g' is multiple: its value must be a list of options"]
    assert errs('g.value = ["Jazz", "Rock"]', multi) == ["line 1: 'Rock' is not one of the options of 'g'"]
    assert errs('g.value = ["Jazz", "Jazz"]', multi) == ["line 1: 'g' lists an option twice"]
    ok = run('g.value = ["Classical", "Jazz"]', multi).state
    assert ok.values["g"] == ["Jazz", "Classical"] and ok.value_set_by["g"] == "agent"      # kept in option order
    assert run('g.value = ["Jazz", "Classical"]', ok).changed == Changed()
    pairs = run('root = Column([g])\ng = Select("Genre", [["j", "Jazz"], ["l", "Lo-fi"]], "l")').state
    assert errs('g.value = "Jazz"', pairs) == ["line 1: 'Jazz' is not one of the options of 'g'"]
    assert run('g.value = "j"', pairs).state.values == {"g": "j"}


def test_a_message_echoes_a_few_characters_of_what_the_agent_sent_not_all_of_it():
    # an agent's own text goes back into its context in the error: a few characters tell it which, a page would drown it
    word = "z" * 500
    clipped = "z" * 32 + "..."
    text_props = "Text(text, style?); every element also takes hidden, disabled"
    assert errs(f'root = Column([t])\nt = Text("x", {word}=1)') == [f"line 2: Text has no property '{clipped}' ({text_props})"]
    assert errs(f'root = Column([t])\nt = Text("x")\nt.{word} = 1') == [f"line 3: Text has no property '{clipped}' ({text_props})"]
    assert errs(f'root = Column([t])\nt = Q{word}("x")') == [f"line 2: unknown element type 'Q{'z' * 31}...' ({TYPES})"]
    s = run('root = Column([g])\ng = Select("Genre", ["Jazz", "Lo-fi"], "Jazz")').state
    assert errs(f'g.value = "{word}"', s) == [f"line 1: '{clipped}' is not one of the options of 'g'"]
    assert errs(f'root = Column([g])\ng = Select("Genre", ["Jazz"], "{word}")') == [f"line 2: '{clipped}' is not one of the options of 'g'"]
    multi = run('root = Column([g])\ng = Select("Genre", ["Jazz", "Lo-fi"], multiple=true)').state
    assert errs(f'g.value = ["Jazz", "{word}"]', multi) == [f"line 1: '{clipped}' is not one of the options of 'g'"]
    assert errs("g.value = [a]", multi) == ["line 1: 'a' is not one of the options of 'g'"]          # a reference is its id, not Ref(id='a')
    assert errs("g.value = 5", s) == ["line 1: '5' is not one of the options of 'g'"]


def test_unknown_property_names_the_common_props():
    expected = "Button(label, style?, disabled?, wake?); every element also takes hidden, disabled"
    assert errs('root = Column([b])\nb = Button("Go", colour="red")') == [f"line 2: Button has no property 'colour' ({expected})"]
    assert errs("play.colour = 1", _built()) == [f"line 1: Button has no property 'colour' ({expected})"]
    ok = run('root = Column([t])\nt = Text("x", hidden=true, disabled=true)').state
    assert ok.elements["t"]["props"] == {"text": "x", "hidden": True, "disabled": True}


def test_script_problems_become_line_errors():
    r = apply_batch(S, parse_program('root = Column([v])\nv = Html("<script>x</script>")'), limits=L,
                    script_check=lambda html: ["script 1 does not parse: Unexpected token ';'"])
    assert [e.render() for e in r.errors] == ["line 2: v: script 1 does not parse: Unexpected token ';'"]


def test_the_script_check_runs_on_new_html_only_and_every_problem_is_an_error():
    seen = []

    def check(html):
        seen.append(html)
        return ["first", "second"] if "bad" in html else []

    s = apply_batch(S, parse_program('root = Column([v])\nv = Html("<b>ok</b>", 50)'), limits=L, script_check=check).state
    assert seen == ["<b>ok</b>"]
    apply_batch(s, parse_program("v = Html(..., 60)"), limits=L, script_check=check)          # kept, so not rechecked
    assert seen == ["<b>ok</b>"]
    apply_batch(s, parse_program('v.html = "<i>new</i>"'), limits=L, script_check=check)
    assert seen == ["<b>ok</b>", "<i>new</i>"]
    r = apply_batch(s, parse_program('v.html = "bad"\nv = Html("bad too")'), limits=L, script_check=check)
    assert [e.render() for e in r.errors] == ["line 1: v: first", "line 1: v: second", "line 2: v: first", "line 2: v: second"]
    # an oversized body is refused before it is checked
    seen.clear()
    big = apply_batch(S, parse_program('root = Column([v])\nv = Html("' + "x" * 70000 + '")'), limits=L, script_check=check)
    assert seen == [] and [e.render() for e in big.errors] == ["line 2: v: Html is 70000 bytes; the limit is 65536"]


# --- binding: arguments, defaults and what a statement may say ------------------------


BUTTON_SIG = "Button(label, style?, disabled?, wake?)"


def test_argument_count_names_and_duplicates():
    assert errs('root = Column([t])\nt = Text("x", "h1", "extra")') == ["line 2: Text takes at most 2 arguments; got 3 (Text(text, style?))"]
    assert errs('root = Column([d])\nd = Divider("x")') == ["line 2: Divider takes no arguments; got 1 (Divider())"]
    assert errs('root = Column([b])\nb = Button("Go", "primary", style="danger")') == [f"line 2: 'style' is given twice ({BUTTON_SIG})"]
    assert errs('root = Column([b])\nb = Button("Go", disabled=true, disabled=false)') == [f"line 2: 'disabled' is given twice ({BUTTON_SIG})"]
    assert errs('root = Column([b])\nb = Button("Go", "primary", true, disabled=true)') == [f"line 2: 'disabled' is given twice ({BUTTON_SIG})"]


def test_required_arguments_and_their_types():
    assert errs("root = Column([b])\nb = Button()") == [f"line 2: Button needs label ({BUTTON_SIG})"]
    assert errs("root = Column([b])\nb = Button(null)") == [f"line 2: Button needs label ({BUTTON_SIG})"]
    assert errs("root = Column([c])\nc = Column()") == ["line 2: Column needs children as a list (Column(children, gap?, align?))"]
    assert errs('root = Column([c])\nc = Column("a")') == ["line 2: Column needs children as a list (Column(children, gap?, align?))"]
    assert errs('root = Column([c])\nc = Column(["a"])') == [
        "line 2: children must be element ids written without quotes, for example [a, b] (Column(children, gap?, align?))"
    ]
    assert errs('root = Column([g])\ng = Select("Genre")') == [f"line 2: Select needs options as a list ({SELECT_SIG})"]
    assert errs("root = Column([i])\ni = Image()") == [
        "line 2: Image needs src (Image(src, alt, fit?, height?))",
        "line 2: Image needs alt (Image(src, alt, fit?, height?))",
    ]
    assert errs('root = Column([i])\ni = Image("https://cdn.example.com/a.png")') == ["line 2: Image needs alt (Image(src, alt, fit?, height?))"]


def test_argument_values_are_judged_with_the_signature_attached():
    assert errs("root = Column([b])\nb = Button(5)") == [f"line 2: label must be text ({BUTTON_SIG})"]
    assert errs('root = Column([b])\nb = Button("x", "loud")') == [f"line 2: style must be one of primary, secondary, danger ({BUTTON_SIG})"]
    assert errs("root = Column([c])\nc = Column([], 100)") == ["line 2: gap must be a number from 0 to 64 (Column(children, gap?, align?))"]
    assert errs('root = Column([t])\nt = Text("x", wrap=true)')[0].startswith("line 2: Text has no property 'wrap' (Text(text, style?);")
    assert errs('root = Column([g])\ng = Select("Genre", ["a", "a"])') == [
        f"line 2: option values must be unique; 'a' appears twice ({SELECT_SIG})"
    ]
    assert errs('root = Column([g])\ng = Select("Genre", [a, b])') == [
        f"line 2: each option must be a string or a [value, label] pair of strings ({SELECT_SIG})"
    ]
    two = errs('root = Column([b])\nb = Button(5, "loud")')
    assert two == [f"line 2: label must be text ({BUTTON_SIG})", f"line 2: style must be one of primary, secondary, danger ({BUTTON_SIG})"]


def test_an_optional_null_means_omitted_and_booleans_may_be_positional():
    r = run('root = Column([t, b])\nt = Text("x", null)\nb = Button("Go", "primary", true)')
    assert r.state.elements["t"]["props"] == {"text": "x"}
    assert r.state.elements["b"]["props"] == {"label": "Go", "style": "primary", "disabled": True}


def test_a_bare_id_cannot_be_a_value_anywhere_but_the_children():
    assert errs('root = Column([v])\nv = Html("x")\nv.value = [v]') == [
        'line 3: a bare id is only allowed inside a children list; quote text as "..."'
    ]


def test_replacing_resets_every_property_that_was_not_sent_again():
    s = run(G("playlist_create.cl")).state
    r = run('play = Button("Play")', s)
    assert r.state.elements["play"] == {"type": "Button", "props": {"label": "Play"}}
    assert r.changed == Changed((), ("play",), ())


# --- values: who set them, what survives, what is refused -------------------------------


def test_the_original_definition_sent_again_keeps_the_users_choice_and_changes_nothing():
    s = _built()
    r = run('genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")', s)
    assert (r.state.values, r.state.value_set_by) == ({"genre": "Lo-fi"}, {"genre": "user"})
    assert r.changed == Changed() and r.warnings == ()


def test_an_agent_set_value_does_not_survive_a_replacement():
    s = run('genre.value = "Classical"', _built()).state
    r = run('genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")', s)
    assert r.state.values == {} and r.state.value_set_by == {} and r.warnings == ()


def test_a_multiple_choice_is_kept_in_the_new_option_order():
    s = run('root = Column([g])\ng = Select("Tags", ["a", "b", "c"], multiple=true)').state
    s.values["g"], s.value_set_by["g"] = ["a", "c"], "user"
    r = run('g = Select("Tags", ["c", "b", "a"], multiple=true)', s)
    assert r.state.values == {"g": ["c", "a"]} and r.state.value_set_by == {"g": "user"}
    gone = run('g = Select("Tags", ["c", "b", "a"])', s)           # no longer multiple: a list is not valid
    assert gone.state.values == {} and gone.warnings == ("g: the user's value was reset because the element changed",)


def test_audio_state_survives_a_replacement_only_when_the_track_is_the_same():
    s = run(G("playlist_create.cl")).state
    s.values["player"], s.value_set_by["player"] = {"playing": True, "position": 42.0}, "user"
    same = run('player = Audio("https://cdn.example.com/audio/so-what.mp3", "Renamed")', s)
    assert same.state.values["player"] == {"playing": True, "position": 42.0}
    other = run('player = Audio("https://cdn.example.com/audio/rainy-day.mp3", "Rainy Day")', s)
    assert "player" not in other.state.values
    assert other.warnings == ("player: the user's value was reset because the element changed",)


def test_an_html_value_survives_a_replacement_of_the_same_type():
    s = run(G("playlist_create.cl")).state
    s.values["viz"], s.value_set_by["viz"] = {"n": 1}, "user"
    r = run('viz = Html("<b>new</b>", 70)', s)
    assert r.state.values == {"viz": {"n": 1}} and r.state.value_set_by == {"viz": "user"}


def test_only_select_and_html_have_a_value():
    s = _built()
    assert errs("title.value = 1", s) == ["line 1: Text has no value"]
    assert errs("player.value = 1", s) == ["line 1: Audio has no value"]
    assert errs("play.value = true", s) == ["line 1: Button has no value"]
    assert errs("controls.value = 1", s) == ["line 1: Row has no value"]


def test_an_html_value_is_json_of_at_most_a_thousand_characters():
    s = run(G("playlist_create.cl")).state
    r = run('viz.value = {"a": [1, 2.5, "x", null, true]}', s)
    assert r.state.values == {"viz": {"a": [1, 2.5, "x", None, True]}} and r.state.value_set_by == {"viz": "agent"}
    assert run('viz.value = "x"', r.state).state.values == {"viz": "x"}
    assert run('viz.value = {"a": [1, 2.5, "x", null, true]}', r.state).changed == Changed()
    ok = run('viz.value = "' + "y" * 998 + '"', s)
    assert len(json.dumps(ok.state.values["viz"])) == 1000
    assert errs('viz.value = "' + "y" * 999 + '"', s) == ["line 1: Html value is 1001 characters of JSON; the limit is 1000"]
    assert errs('viz.value = "a\\u0000b"', s) == ["line 1: value contains a character that cannot be stored (a NUL or a lone surrogate)"]
    cleared = run("viz.value = null", r.state)
    assert cleared.state.values == {} and cleared.state.value_set_by == {} and cleared.changed == Changed((), ("viz",), ())
    assert run("viz.value = null", s).changed == Changed()

    # A value the parser would refuse is refused here too, or canvas_get could print a program that cannot be read back
    # (design 8.4). The parser reads integers of at most 15 digits and nests lists and objects at most 32 levels, and a
    # hand-built statement, which is what a JSON op becomes, never meets the parser: so the batch holds it to the same.
    def refused(value):
        result = apply_batch(s, [SetProp(1, "viz", "value", value)], limits=L)
        assert result.state is None
        return [e.render() for e in result.errors]

    out_of_range = ["line 1: number is out of range"]
    assert refused(10**15) == refused(-(10**15)) == refused(10**16) == refused(10**400) == out_of_range
    assert refused(10**5000) == refused(-(10**5000)) == out_of_range             # too long even to print as text
    assert refused([[10**15]]) == refused({"k": [10**16]}) == out_of_range
    too_deep = ["line 1: lists and objects are nested more than 32 levels deep"]
    assert refused(nested(33)) == refused(nested(33, "o")) == refused(nested(33, "lo")) == refused(nested(200)) == too_deep
    assert refused(float("nan")) == refused(float("inf")) == ["line 1: value must be JSON: no NaN or infinity"]
    for value in (10**15 - 1, -(10**15 - 1), nested(32), nested(32, "o"), nested(32, "lo")):
        accepted = apply_batch(s, [SetProp(1, "viz", "value", value)], limits=L)
        assert accepted.errors == () and accepted.state.values == {"viz": value}


def test_a_value_json_cannot_measure_is_a_line_error_never_a_crash(monkeypatch):
    # check_json_value bounds what json.dumps can meet (an integer of 4301 digits makes it raise ValueError), so this
    # cannot happen; if it ever does, one agent's value must not be able to crash a batch
    from types import SimpleNamespace

    import primer.canvas.state as state_module

    s = run(G("playlist_create.cl")).state

    def refuse(*args, **kwargs):
        raise ValueError("Exceeds the limit (4300 digits) for integer string conversion")

    monkeypatch.setattr(state_module, "json", SimpleNamespace(dumps=refuse))
    r = apply_batch(s, [SetProp(1, "viz", "value", 5)], limits=L)
    assert r.state is None
    assert [e.render() for e in r.errors] == ["line 1: value must be JSON (text, numbers, true, false, null, lists and objects)"]


def test_check_json_value_is_the_one_judge_of_a_value():
    from primer.canvas.lang import MSG_BARE_ID, Ref

    assert check_json_value({"a": [1, 2.5, "x", None, True, {"b": []}]}) is None
    assert check_json_value(10**15 - 1) is None and check_json_value(-(10**15 - 1)) is None
    assert check_json_value(10**15) == check_json_value(10**5000) == "number is out of range"       # compared, never turned into text
    assert check_json_value(nested(32)) is None
    assert check_json_value(nested(33)) == check_json_value(nested(200)) == "lists and objects are nested more than 32 levels deep"
    assert check_json_value(float("nan")) == "value must be JSON: no NaN or infinity"
    assert check_json_value({1: 2}) == "value must be JSON: object keys are strings"
    assert check_json_value(Ref("a")) == MSG_BARE_ID
    assert check_json_value(object()) == "value must be JSON (text, numbers, true, false, null, lists and objects)"
    cannot = "value contains a character that cannot be stored (a NUL or a lone surrogate)"
    assert check_json_value("a\x00b") == check_json_value(["ok", {"k": "\ud800"}]) == cannot


def test_clearing_a_select_value_goes_back_to_its_initial_value():
    r = run("genre.value = null", _built())
    assert r.state.values == {} and r.state.value_set_by == {}
    assert r.state.current_value("genre") == "Jazz" and r.changed == Changed((), ("genre",), ())


def test_changing_the_options_resets_or_refuses_the_values():
    s = _built()
    r = run('genre.options = ["Jazz", "Classical"]', s)
    assert r.state.elements["genre"]["props"]["options"] == ["Jazz", "Classical"]
    assert r.state.values == {} and r.warnings == ("genre: the user's value was reset because the element changed",)
    # the initial value is the agent's own, so a conflict is an error rather than a silent reset
    assert errs('genre.options = ["Rock"]', s) == ["line 1: 'Jazz' is not one of the options of 'genre'"]
    assert errs("genre.multiple = true", s) == ["line 1: 'genre' is multiple: its value must be a list of options"]
    assert errs('genre.options = ["a", "a"]', s) == [f"line 1: option values must be unique; 'a' appears twice ({SELECT_SIG})"]
    ok = run('genre.options = ["Jazz", "Lo-fi", "Blues"]', s)
    assert ok.state.values == {"genre": "Lo-fi"} and ok.warnings == ()


# --- other statements -------------------------------------------------------------------


def test_append_and_the_children_property():
    s = _built()
    assert errs("title += [cover]", s) == ["line 1: Text cannot have children; only Column, Row and Card can"]
    assert errs("nope += [title]", s) == ["line 1: no element named 'nope'"]
    assert errs("controls += [title]", s) == ["line 1: 'title' is already a child of 'root'"]
    added = run('extra = Text("e")\ncontrols += [extra]', s)
    assert added.state.elements["controls"]["children"] == ["genre", "play", "extra"]
    assert added.changed == Changed(("extra",), ("controls",), ())
    reordered = run("controls.children = [play, genre]", s)
    assert reordered.state.elements["controls"]["children"] == ["play", "genre"] and reordered.changed == Changed((), ("controls",), ())
    row = "Row(children, gap?, align?, wrap?)"
    assert errs("controls.children = null", s) == [f"line 1: Row needs children as a list ({row})"]
    assert errs('controls.children = ["a"]', s) == [f"line 1: children must be element ids written without quotes, for example [a, b] ({row})"]
    assert errs("title.children = [genre]", s)[0].startswith("line 1: Text has no property 'children' (Text(text, style?);")


def test_ids_in_hand_built_statements_are_checked_too():
    from primer.canvas.lang import Append, Assign, Delete, Ref, SetProp

    rule = "(a lowercase letter, then lowercase letters, digits or _, at most 32)"

    def refused(*statements, **kw):
        result = apply_batch(S, list(statements), limits=L, **kw)
        assert result.state is None
        return [e.render() for e in result.errors]

    assert refused(Assign(1, "Bad Id", "Text", ("x",), ())) == [f"line 1: 'Bad Id' is not an element id {rule}"]
    assert refused(Assign(1, "true", "Text", ("x",), ())) == ["line 1: 'true' is reserved (it means a value); pick another element id"]
    assert refused(SetProp(2, "Bad", "x", 1)) == [f"line 2: 'Bad' is not an element id {rule}"]
    assert refused(Append(1, "root", ("Bad",))) == [f"line 1: 'Bad' is not an element id {rule}"]
    assert refused(Delete(3, ("Bad",)), unit="op") == [f"op 3: 'Bad' is not an element id {rule}"]
    assert refused(Assign(1, "col", "Column", ([Ref("Bad")],), ())) [0].startswith("line 1: 'Bad' is not an element id")
    with pytest.raises(TypeError):
        apply_batch(S, [object()], limits=L)


def test_the_unit_is_op_for_json_ops():
    r = apply_batch(S, parse_program('a = Buton("x")'), limits=L, unit="op")
    assert [e.render() for e in r.errors] == [f"op 1: unknown element type 'Buton' ({TYPES})"]


def test_final_errors_are_listed_in_program_order():
    assert errs('orphan = Text("x")\nroot = Column([ghost])') == [
        "line 1: 'orphan' is not reachable from root; add it to a container's children",
        "line 2: unknown element 'ghost' in the children of 'root'",
    ]


def test_statement_errors_stop_the_whole_batch_before_the_structure_is_judged():
    assert errs('root = Column([a, ghost])\na = Buton("x")') == [f"line 2: unknown element type 'Buton' ({TYPES})"]


def test_a_flood_of_errors_is_capped_and_counted():
    result = run("\n".join(f'a{i} = Buton("x")' for i in range(60)))
    assert len(result.errors) == 51
    assert result.errors[50].render() == "line 51: 10 more errors are not shown"


# --- the contract of apply_batch ----------------------------------------------------------


def test_apply_batch_never_mutates_its_input_or_aliases_the_statements():
    s = _built()
    frozen = copy.deepcopy(s)
    statements = parse_program('genre = Select("Genre", ["Jazz"])\nroot += [x]\nx = Text("x")\ndel play\nclear\nroot = Column([y])\ny = Divider()')
    apply_batch(s, statements, limits=L)
    apply_batch(s, parse_program('a = Buton("x")'), limits=L)
    assert s == frozen
    options = parse_program('root = Column([g])\ng = Select("Genre", ["a", "b"])')
    first = apply_batch(S, options, limits=L).state
    first.elements["g"]["props"]["options"].append("zzz")
    second = apply_batch(S, options, limits=L).state
    assert second.elements["g"]["props"]["options"] == ["a", "b"]


def test_the_same_program_applied_twice_changes_nothing_the_second_time():
    first = run(G("playlist_create.cl"))
    again = run(G("playlist_create.cl"), first.state)
    assert again.changed == Changed() and again.state == first.state


def test_an_empty_batch_is_a_no_op():
    s = _built()
    r = apply_batch(s, [], limits=L)
    assert r.state == s and r.state is not s and r.changed == Changed() and r.errors == ()


def test_changed_lists_ids_in_a_stable_order():
    s = _built()
    r = run('extra = Text("e")\nroot += [extra]\ntitle = Text("New title", "h1")\ndel more, viz', s)
    assert r.changed == Changed(("extra",), ("root", "title"), ("more", "viz"))
    assert r.changed.summary() == "+1 ~2 -2"


def test_rev_is_left_to_the_service():
    s = _built()
    assert run('title = Text("New")', s).state.rev == s.rev == 3


def test_setting_a_value_to_the_initial_one_is_no_change_but_overriding_a_users_choice_is():
    fresh = run(G("playlist_create.cl")).state
    same = run('genre.value = "Jazz"', fresh)
    assert same.state.values == {} and same.changed == Changed()
    over = run('genre.value = "Jazz"', _built())
    assert over.state.values == {"genre": "Jazz"} and over.state.value_set_by == {"genre": "agent"}


def test_a_bool_is_not_a_number_when_values_are_compared():
    from primer.canvas.state import json_equal

    assert json_equal(1, 1.0) and json_equal([1, "a"], (1, "a")) and json_equal({"a": [True]}, {"a": [True]})
    assert not json_equal(True, 1) and not json_equal(0, False) and not json_equal([True], [1]) and not json_equal("1", 1)
    assert not json_equal({"a": 1}, {"a": 1, "b": 2}) and not json_equal(None, 0)


def test_a_change_is_a_change_of_the_elements_effective_value_not_of_a_stored_entry():
    s = run('genre.value = "Jazz"', _built()).state                 # the agent sets the choice back to the initial value
    assert s.values == {"genre": "Jazz"} and s.value_set_by == {"genre": "agent"}
    r = run('genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")', s)
    assert r.state.values == {} and r.changed == Changed()          # the entry went, what the element shows did not
    assert run('genre.value = "Classical"', s).changed == Changed((), ("genre",), ())
