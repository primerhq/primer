"""Canvas Lang syntax: statements, values and line-numbered errors.

The two golden programs are the research report's examples (d.5), verbatim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from primer.canvas.lang import (
    KEEP,
    Append,
    Assign,
    CanvasLangError,
    Clear,
    Delete,
    LangError,
    Ref,
    SetProp,
    format_value,
    parse_program,
)

GOLDEN = Path(__file__).parent / "golden"


def G(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


def test_the_playlist_program_parses():
    stmts = parse_program(G("playlist_create.cl"))
    assert [s.id for s in stmts] == ["root", "title", "cover", "controls", "genre", "play", "player", "more", "viz"]
    assert stmts[0] == Assign(1, "root", "Column", ([Ref("title"), Ref("cover"), Ref("controls"), Ref("player"), Ref("more"), Ref("viz")],), ())
    assert stmts[4].args == ("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
    assert stmts[8].args == ("<div id=b></div><script>b.textContent='3 tracks queued'</script>", 60)


def test_the_update_program_uses_every_update_form():
    assert parse_program(G("playlist_update.cl")) == [
        SetProp(1, "cover", "src", "https://cdn.example.com/covers/lofi-beats.jpg"),
        Assign(2, "player", "Audio", ("https://cdn.example.com/audio/rainy-day.mp3", "Rainy Day - Lo-fi Beats"), ()),
        SetProp(3, "play", "disabled", True),
        Assign(4, "skip", "Button", ("Skip",), ()),
        Append(5, "controls", ("skip",)),
    ]


def test_named_args_triple_quotes_comments_keep_del_and_clear():
    stmts = parse_program('# heading\nviz = Html("""<b>\n"hi"</b>""", height=60)  # note\nviz = Html(..., 80)\ndel a, b\nclear\n')
    assert stmts[0] == Assign(2, "viz", "Html", ('<b>\n"hi"</b>',), (("height", 60),))
    assert stmts[1].args == (KEEP, 80)
    assert stmts[2:] == [Delete(5, ("a", "b")), Clear(6)]


@pytest.mark.parametrize("text, rendered", [
    ('Title = Text("x")', "line 1: 'Title' is not an element id"),
    ('a = Text("x)', "line 1: unterminated string"),
    ('a = Text(title)', "line 1: a bare id is only allowed inside a children list"),
    ('a = Button(style="primary", "Go")', "line 1: positional argument after a named one"),
    ('a b c', "line 1: not a statement"),
    ('a = Text("x") extra', "line 1: unexpected text after the statement"),
    ('a = Text([...])', "line 1: ... keeps a current value and must be a whole argument"),
])
def test_syntax_errors_name_the_line(text, rendered):
    with pytest.raises(CanvasLangError) as exc:
        parse_program(text)
    assert exc.value.errors[0].render().startswith(rendered)


def test_every_bad_line_is_reported():
    with pytest.raises(CanvasLangError) as exc:
        parse_program('a = Text("ok")\nb = \nc = Text("ok")\nd.x = \n')
    assert [e.line for e in exc.value.errors] == [2, 4]


def test_single_quotes_are_accepted():
    assert parse_program("t = Text('it\\'s')")[0].args == ("it's",)


def test_format_value_is_canonical():
    assert [format_value(v) for v in ("Lo-fi", True, None, 60, 1.5, KEEP)] == ['"Lo-fi"', "true", "null", "60", "1.5", "..."]
    assert format_value([["a", "A"], ["b", "B"]]) == '[["a", "A"], ["b", "B"]]'
    assert format_value([Ref("a"), Ref("b")]) == "[a, b]"
    assert format_value('say "hi"\n') == '"say \\"hi\\"\\n"'


def test_a_lang_error_renders_the_expected_signature():
    err = LangError(4, "Select needs options as a list", "Select(label, options, value?, multiple?, wake?)")
    assert err.render() == "line 4: Select needs options as a list (Select(label, options, value?, multiple?, wake?))"
    assert LangError(2, "bad", unit="op").render() == "op 2: bad"


# --- beyond the plan's tests: every rule of Decision 4 and the edges the parser must hold ---


def _errors(text: str) -> list[str]:
    with pytest.raises(CanvasLangError) as exc:
        parse_program(text)
    return [e.render() for e in exc.value.errors]


def test_keep_is_a_singleton_that_survives_copying_and_pickling():
    import copy
    import pickle

    from primer.canvas.lang import Keep

    assert Keep() is KEEP and repr(KEEP) == "..."
    assert copy.copy(KEEP) is KEEP and copy.deepcopy([KEEP]) == [KEEP]
    assert copy.deepcopy([KEEP])[0] is KEEP
    assert pickle.loads(pickle.dumps(KEEP)) is KEEP


def test_a_keep_is_allowed_as_a_whole_argument_a_named_value_or_a_property_value():
    stmts = parse_program('a = Html(..., height=...)\nb.height = ...\nc = Image(..., ..., "cover")')
    assert stmts[0].args == (KEEP,) and stmts[0].kwargs == (("height", KEEP),)
    assert stmts[1] == SetProp(2, "b", "height", KEEP)
    assert stmts[2].args == (KEEP, KEEP, "cover")


@pytest.mark.parametrize("text", ["a = Text(...x)", "a = Text(... 5)", "a = Text([1, ...])", 'a = Text({"k": ...})', "a += [...]", "a.x = [...]"])
def test_a_keep_anywhere_but_a_whole_argument_is_refused(text):
    assert _errors(text) == ["line 1: ... keeps a current value and must be a whole argument"]


def test_clear_and_del_can_also_be_element_ids():
    stmts = parse_program('clear = Button("Clear")\ndel = Button("Delete")\nclear.disabled = true\ndel del, clear\n')
    assert [type(s).__name__ for s in stmts] == ["Assign", "Assign", "SetProp", "Delete"]
    assert stmts[0].id == "clear" and stmts[1].id == "del"
    assert stmts[3] == Delete(4, ("del", "clear"))


def test_clear_takes_nothing_after_it():
    assert parse_program("clear  # all gone") == [Clear(1)]
    assert _errors("clear all") == ["line 1: unexpected text after the statement: all"]


def test_del_needs_ids():
    assert _errors("del") == [
        "line 1: not a statement: expected id = Type(...), id.prop = value, id += [ids], del ids or clear"
    ]
    assert _errors("del a,") == ["line 1: expected an element id"]
    assert _errors("del A") == [
        "line 1: 'A' is not an element id (a lowercase letter, then lowercase letters, digits or _, at most 32)"
    ]
    assert _errors("del a b") == ["line 1: unexpected text after the statement: b"]


def test_element_ids_follow_the_rule_and_the_reserved_words_are_refused():
    rule = "(a lowercase letter, then lowercase letters, digits or _, at most 32)"
    assert _errors('my-button = Text("x")') == [f"line 1: 'my-button' is not an element id {rule}"]
    assert _errors('3d = Text("x")') == [f"line 1: '3d' is not an element id {rule}"]
    assert _errors('a' * 33 + ' = Text("x")') == [f"line 1: '{'a' * 32}...' is not an element id {rule}"]
    assert parse_program("a" * 32 + ' = Text("x")')[0].id == "a" * 32
    assert _errors('true = Text("x")') == ["line 1: 'true' is reserved (it means a value); pick another element id"]
    assert _errors("a += [null]") == ["line 1: += takes a list of element ids written without quotes: id += [a, b]"]
    assert _errors("a = Column([Title])") == [f"line 1: 'Title' is not an element id {rule}"]


def test_a_bare_id_is_a_ref_only_inside_a_list():
    assert parse_program("a = Column([b, [c]])")[0].args == ([Ref("b"), [Ref("c")]],)
    assert _errors("a.x = b") == ['line 1: a bare id is only allowed inside a children list; quote text as "..."']
    assert _errors("a = Text(b)") == ['line 1: a bare id is only allowed inside a children list; quote text as "..."']


def test_nested_elements_get_their_own_message():
    assert _errors('root = Column([Text("a")])') == [
        "line 1: elements cannot be nested; give the inner Text its own line (id = Text(...)) and list its id in the parent's children"
    ]
    assert _errors('root = Column(Text ("a"))')[0].startswith("line 1: elements cannot be nested;")


def test_an_append_takes_bare_ids_in_a_list():
    msg = "line 1: += takes a list of element ids written without quotes: id += [a, b]"
    assert _errors("controls += skip") == [msg]
    assert _errors('controls += ["skip"]') == [msg]
    assert parse_program("controls += [a, b,]  # two")[0] == Append(1, "controls", ("a", "b"))
    assert parse_program("controls += []")[0] == Append(1, "controls", ())


def test_a_double_equals_is_not_an_assignment():
    assert _errors("a == b")[0].startswith("line 1: not a statement")
    assert _errors("a.b == 1")[0].startswith("line 1: not a statement")


def test_spacing_is_flexible_and_a_trailing_comma_is_fine():
    assert parse_program('a=Text("x")')[0] == Assign(1, "a", "Text", ("x",), ())
    assert parse_program('  \t a  =  Text ( "x" ,  style = "h1" , )  ')[0] == Assign(1, "a", "Text", ("x",), (("style", "h1"),))
    assert parse_program("a.b=1")[0] == SetProp(1, "a", "b", 1)


def test_unexpected_text_shows_the_first_twenty_characters():
    assert _errors('a = Text("x") 0123456789012345678901234567890') == [
        "line 1: unexpected text after the statement: 01234567890123456789"
    ]
    assert _errors('a = Text("x") extra  # note') == ["line 1: unexpected text after the statement: extra  # note"]


def test_property_and_argument_names_are_lowercase_words():
    assert _errors('a = Button(Style="x")') == [
        "line 1: 'Style' is not a property name (a lowercase letter, then lowercase letters, digits or _)"
    ]
    assert _errors("a.Value = 1") == [
        "line 1: 'Value' is not a property name (a lowercase letter, then lowercase letters, digits or _)"
    ]
    assert _errors("a.b.c = 1")[0].startswith("line 1: not a statement")


def test_a_statement_that_is_cut_short_says_what_is_missing():
    assert _errors('a = Text("x"') == ["line 1: missing ) to close the arguments; each statement must fit on one line"]
    assert _errors('a = Row([b, c)') == ["line 1: expected , or ] in the list, found: )"]
    assert _errors('a = Row([b, c') == ["line 1: missing ] to close the list; each statement must fit on one line"]
    assert _errors('a = Text("x" "y")') == ['line 1: expected , or ) after the argument, found: "y")']
    # a call spread over lines is two bad statements, reported where they are
    assert _errors('genre = Select("Genre",\n  ["Jazz"])') == [
        "line 1: missing ) to close the arguments; each statement must fit on one line",
        "line 2: not a statement: expected id = Type(...), id.prop = value, id += [ids], del ids or clear",
    ]
    assert _errors("a = Text(") == ["line 1: missing ) to close the arguments; each statement must fit on one line"]
    assert _errors("a = Text(,)") == ["line 1: expected a value, found: ,)"]
    assert _errors("a = Text") == [
        "line 1: not a statement: expected id = Type(...), id.prop = value, id += [ids], del ids or clear"
    ]
    assert _errors("a = text('x')")[0].startswith("line 1: not a statement")


def test_the_values_of_a_property_are_required():
    assert _errors("d.x =") == ["line 1: expected a value"]
    assert _errors("d.x = )") == ["line 1: expected a value, found: )"]


# strings


def test_strings_take_json_escapes_and_the_two_quote_kinds():
    a = parse_program(r'a = Text("tab\there \"quoted\" \\ \/ \u00e9 \ud83d\ude00 \'x\'")')[0]
    assert a.args == ('tab\there "quoted" \\ / \u00e9 \U0001F600 \'x\'',)
    assert parse_program("a = Text('say \"hi\" and \\'bye\\'')")[0].args == ('say "hi" and \'bye\'',)
    assert parse_program('a = Text("")')[0].args == ("",)


@pytest.mark.parametrize("escape", [r"\d", r"\q", r"\u12", r"\uZZZZ", r"\ud83d", r"\ude00", r"\ud83dx"])
def test_a_bad_escape_names_the_escape_and_the_way_out(escape):
    (error,) = _errors('a = Text("x' + escape + '")')
    assert error.startswith("line 1: invalid escape ")
    assert error.endswith('write \\\\ for a backslash, or use a """...""" string, which is taken as it is')


def test_a_lone_backslash_at_the_end_of_a_line_is_an_unterminated_string():
    assert _errors('a = Text("x\\\nb = Text("y")') == ["line 1: unterminated string"]


def test_triple_quoted_strings_are_taken_verbatim_and_keep_their_start_line():
    text = 'a = Text("x")\nb = Html("""line one\n  "two" \\n \\d\nthree""", 60)\nc = Text("y")'
    stmts = parse_program(text)
    assert stmts[1] == Assign(2, "b", "Html", ('line one\n  "two" \\n \\d\nthree', 60), ())
    assert stmts[2].line == 5


def test_a_triple_quoted_body_may_end_with_quotes():
    assert parse_program('a = Html("""say "hi"""")')[0].args == ('say "hi"',)
    assert parse_program('a = Html("""""")')[0].args == ("",)
    assert parse_program('a = Html("""<a href="x">""", 5)')[0].args == ('<a href="x">', 5)


def test_an_unterminated_triple_quote_is_one_error_at_its_start():
    assert _errors('a = Text("x")\nb = Html("""never closed\nc = Text("y")\nd = Text("z")') == ["line 2: unterminated string"]


def test_comment_characters_inside_strings_are_text():
    stmts = parse_program('a = Text("# not a comment")  # a comment\nb = Text(\'#\')')
    assert stmts[0].args == ("# not a comment",) and stmts[1].args == ("#",)


# numbers


@pytest.mark.parametrize("text, value", [("0", 0), ("60", 60), ("-5", -5), ("1.5", 1.5), ("-0.25", -0.25), ("1e3", 1000.0), ("2.5E-2", 0.025)])
def test_numbers(text, value):
    got = parse_program(f"a.x = {text}")[0].value
    assert got == value and type(got) is type(value)


@pytest.mark.parametrize("text", ["1e999", "1234567890123456", "-1234567890123456"])
def test_numbers_out_of_range_are_refused(text):
    assert _errors(f"a.x = {text}") == ["line 1: number is out of range"]


def test_a_number_followed_by_letters_is_refused():
    assert _errors('a = Image("x", "y", height=60px)') == ["line 1: expected , or ) after the argument, found: px)"]
    assert _errors("a.x = -") == ["line 1: expected a value, found: -"]


# lists and objects


def test_lists_nest_and_allow_a_trailing_comma():
    assert parse_program('a.x = [[1, 2], ["a", [true, null]],]')[0].value == [[1, 2], ["a", [True, None]]]
    assert parse_program("a.x = []")[0].value == []


def test_nesting_is_bounded():
    ok = "[" * 32 + "]" * 32
    assert parse_program(f"a.x = {ok}")[0].value
    assert _errors("a.x = " + "[" * 33 + "]" * 33) == ["line 1: lists and objects are nested more than 32 levels deep"]
    assert _errors("a.x = " + "[" * 100000) == ["line 1: lists and objects are nested more than 32 levels deep"]


def test_objects_are_json_style():
    assert parse_program('v.value = {"a": 1, \'b\': [1, {"c": null}], "d": "x",}')[0].value == {"a": 1, "b": [1, {"c": None}], "d": "x"}
    assert parse_program("v.value = {}")[0].value == {}
    assert _errors("v.value = {a: 1}") == ["line 1: object keys must be strings in quotes, found: a: 1}"]
    assert _errors('v.value = {"a" 1}') == ["line 1: expected : after the object key, found: 1}"]
    assert _errors('v.value = {"a": 1') == ["line 1: missing } to close the object; each statement must fit on one line"]
    assert _errors('v.value = {"a": 1 "b": 2}') == ['line 1: expected , or } in the object, found: "b": 2}']


# whole programs


def test_line_numbers_count_blank_lines_comments_and_multiline_strings():
    text = '\n# one\n\n  # two\na = Text("""x\ny\nz""")\n\nb = Text("w")\n'
    assert [(s.id, s.line) for s in parse_program(text)] == [("a", 5), ("b", 9)]


def test_windows_line_endings_and_a_byte_order_mark_are_tolerated():
    stmts = parse_program('\ufeffa = Text("x")\r\nb = Html("""p\r\nq""")\rc = Text("y")\r\n')
    assert [s.line for s in stmts] == [1, 2, 4]
    assert stmts[1].args == ("p\nq",)


def test_an_empty_program_has_no_statements():
    assert parse_program("") == [] == parse_program("  \n# only a comment\n\n")


def test_many_errors_are_capped_and_counted():
    errors = _errors("\n".join(f"bad {i}" for i in range(60)))
    assert len(errors) == 51
    assert errors[50] == "line 51: 10 more syntax errors are not shown"


def test_one_error_per_bad_statement_even_when_it_has_two_problems():
    assert _errors('a = Text(b, c, "ok" 1)') == ['line 1: a bare id is only allowed inside a children list; quote text as "..."']


def test_the_error_joins_every_render_with_a_semicolon_and_pickles():
    import pickle

    with pytest.raises(CanvasLangError) as exc:
        parse_program('x y\nb = \n')
    assert str(exc.value) == "line 1: not a statement: expected id = Type(...), id.prop = value, id += [ids], del ids or clear; line 2: not a statement: expected id = Type(...), id.prop = value, id += [ids], del ids or clear"
    again = pickle.loads(pickle.dumps(exc.value))
    assert again.errors == exc.value.errors and str(again) == str(exc.value)


# canonical literals


@pytest.mark.parametrize("value", [
    "plain", "", 'with "quotes" and \\ backslash', "it's", "multi\nline\ttab", "caf\u00e9 \U0001F600", "# hash", "\u2028",
    0, -3, 60, 1.5, -0.25, 1e-07, 1e16, 0.1,
    True, False, None,
    [], [1, "a", None, [True]], ["Jazz", "Lo-fi"], [["a", "A"], ["b", "B"]],
    {}, {"a": 1, "b": [1, {"c": None}]}, {"k": "v \"q\""},
])
def test_format_value_round_trips_through_the_parser(value):
    parsed = parse_program(f"a.x = {format_value(value)}")[0].value
    assert parsed == value and type(parsed) is type(value)


def test_format_value_never_uses_single_quotes_and_refuses_what_it_cannot_write():
    assert format_value("it's") == '"it\'s"'
    assert format_value({"a": [Ref("x"), 1]}) == '{"a": [x, 1]}'
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            format_value(bad)
    with pytest.raises(TypeError):
        format_value(object())
    with pytest.raises(ValueError):
        format_value([[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]])
