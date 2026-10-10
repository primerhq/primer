"""The lexer the static scans over ``ui/`` read the source with (``tests/_support/js_source.py``): comments, strings and template text taken out, offsets kept."""

from __future__ import annotations

import pytest

from tests._support.js_source import blank, close_of, line_of, value_end


def test_the_result_has_the_same_length_and_the_same_lines() -> None:
    src = "a(); // note\n/* one\n   two */ b('x', `y ${z}`);\n"

    for kwargs in ({}, {"strings": True}, {"strings": True, "templates": True}):
        out = blank(src, **kwargs)
        assert len(out) == len(src) and [c == "\n" for c in out] == [c == "\n" for c in src], kwargs


def test_comments_are_blanked_and_code_after_them_is_kept() -> None:
    out = blank("a(); // err.envelope\n/* err.envelope\n */ b();")

    assert "envelope" not in out and "a();" in out and "b();" in out


def test_a_double_slash_inside_a_string_is_not_a_comment() -> None:
    out = blank('const u = "http://x.test/a"; const e = err.envelope;')

    assert "http://x.test/a" in out and "err.envelope" in out


def test_strings_are_blanked_only_when_asked_to() -> None:
    src = "f('a.envelope', \"b.envelope\")"

    assert blank(src).count("envelope") == 2
    assert "envelope" not in blank(src, strings=True)
    assert blank(src, strings=True).startswith("f('")


def test_template_text_is_blanked_but_the_placeholders_stay_code() -> None:
    src = "x = `Save (${name}) failed (${err.code}) code`;"

    out = blank(src, strings=True, templates=True)

    assert "name" in out and "err.code" in out
    assert "Save" not in out and "failed" not in out and out.rstrip().endswith("`;")
    assert "Save" in blank(src, strings=True)


def test_a_nested_template_and_braces_in_a_placeholder_do_not_end_it_early() -> None:
    src = "x = `a ${ f({ k: 1 }, `inner ${deep.code} text`) } tail`; y = err.envelope;"

    out = blank(src, strings=True, templates=True)

    assert "deep.code" in out and "inner" not in out and "tail" not in out
    assert "err.envelope" in out, "the code after the template is still code"


def test_an_escaped_quote_does_not_end_a_string() -> None:
    out = blank("a('it\\'s code', b.envelope)", strings=True)

    assert "code" not in out and "b.envelope" in out


def test_a_quote_in_jsx_text_swallows_at_most_the_rest_of_its_line() -> None:
    out = blank("<p>Don't save</p>\nconst e = err.envelope;\n", strings=True)

    assert "err.envelope" in out


@pytest.mark.parametrize(
    ("text", "open_at", "expected"),
    [("f(a, (b), [c])", 1, 14), ("{ a: { b } } tail", 0, 12), ("(unclosed", 0, -1)],
)
def test_close_of_returns_the_offset_past_the_matching_bracket(text: str, open_at: int, expected: int) -> None:
    assert close_of(text, open_at) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("a ? b : c, d", "a ? b : c"), ("f(x, y), z", "f(x, y)"), ("a }", "a "), ("a)", "a"), ("a; b", "a"), ("whole", "whole")],
)
def test_value_end_stops_at_a_top_level_comma_or_the_closing_bracket(text: str, expected: str) -> None:
    assert text[: value_end(text, 0)] == expected


def test_line_of_counts_from_one() -> None:
    assert [line_of("a\nb\nc", i) for i in (0, 2, 4)] == [1, 2, 3]
