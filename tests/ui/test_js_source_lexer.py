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


# ---- regular-expression literals (review of #720, round 1, B1) ------------------------------------------------------------------------------------------


def test_a_regex_literal_with_a_backtick_does_not_open_a_template() -> None:
    """knowledge.jsx:139 ``/(^|\\n)(#{1,6} |\\* |- |\\d+\\. |```)/`` opened a template at its first backtick: 16k of the file's 41k characters were blanked and the scans read nothing after it."""
    src = "const heading = /(^|\\n)(#{1,6} |\\* |- |\\d+\\. |```)/;\nconst e = err.envelope;\nconst b = <Banner title={`Failed (${err.code})`} />;\n"

    out = blank(src, strings=True, templates=True)

    assert "err.envelope" in out and "err.code" in out, out


def test_a_regex_literal_with_a_quote_inside_a_placeholder_does_not_desync_the_frames() -> None:
    """predicate-builder.jsx:160 ``.replace(/'/g, ..)`` inside a placeholder: the quote opened a string, the rest of the line was swallowed, the placeholder's closing brace never came."""
    src = "const t = `a ${s.replace(/'/g, \"\")} b`; const e = err.envelope; const u = `x ${err.code}`;\n"

    out = blank(src, strings=True, templates=True)

    assert "err.envelope" in out and "err.code" in out, out
    assert " a " not in out and " b" not in out.split("err.envelope")[0], "the template's own text is still blanked"


@pytest.mark.parametrize(
    "prefix",
    [
        "", "(", ", ", "= ", ": ", "[", "!", "&&", "||", "? ", "{", "}", ";", "return ", "typeof ", "case ", "x of ", "x in ",
        # an arrow function's concise body (review of #720, round 2, N2): ``(s) => /"/.test(s)`` was read as a division and the quote hid the rest of its line
        "=> ", "=>",
        # the keywords the module docstring names (round 2, N4: only the first eight were in this matrix)
        "void ", "delete ", "throw ", "else ", "do ", "yield ", "await ", "x instanceof ",
    ],
    ids=lambda p: repr(p),
)
def test_a_regex_literal_is_read_after_every_token_that_allows_one(prefix: str) -> None:
    src = f"{prefix}/`['\"]/.test(x)\nconst e = err.envelope;\n"

    out = blank(src, strings=True, templates=True)

    assert "err.envelope" in out, (prefix, out)


@pytest.mark.parametrize(
    "src",
    ["const q = a / b / c; const e = err.envelope;", "const q = f(x) / 2 / 'y'; const e = err.envelope;", "const q = n++ / 2; const e = err.envelope;", "const q = arr[0] / 2; const e = err.envelope;"],
)
def test_a_slash_that_divides_is_not_a_regex(src: str) -> None:
    out = blank(src, strings=True, templates=True)

    assert "err.envelope" in out, out
    assert "'y'" not in out or "y" not in out.split("'y'")[0], "a string after a division is still a string"


def test_a_jsx_self_closing_tag_after_a_brace_is_not_a_regex() -> None:
    src = '<A b={x} /><B c="y" />\nconst e = err.envelope;\n'

    out = blank(src, strings=True, templates=True)

    assert "<B c=" in out and "err.envelope" in out, out
    assert "y" not in out.split("<B c=")[1].split("/>")[0], "the string inside the second tag is blanked"


def test_a_slash_in_jsx_text_after_a_tag_is_not_a_regex() -> None:
    out = blank("<p>and/or '</p>\nconst e = err.envelope;\n", strings=True, templates=True)

    assert "err.envelope" in out, out


def test_a_regex_does_not_run_past_the_end_of_its_line() -> None:
    """No closing slash on the line: it is not a regex literal, and nothing after it is blanked. The slash on the NEXT line (``a / b``) is what a scan that ran past the newline would take
    for the closer, so without it the test would pass with the newline stop removed (review of #720, round 2, N4)."""
    out = blank("x = a ? /not closed\nconst e = err.envelope; const r = a / b;\n", strings=True, templates=True)

    assert "err.envelope" in out and "a / b" in out, out


def test_an_escaped_slash_and_an_escaped_quote_in_a_regex_body_do_not_end_it_or_open_a_string() -> None:
    """``/\\/\\'/``: the first backslash-slash is not the closer and the backslash-quote is not a string."""
    out = blank("const r = /\\/\\'/; const e = err.envelope;\n", strings=True, templates=True)

    assert "err.envelope" in out, out


def test_a_regex_after_an_arrow_hides_neither_its_quote_nor_the_rest_of_its_line() -> None:
    """The case of review #720 round 2, N2: a concise arrow body that is a regex literal, with a banner after it on the same line."""
    src = 'const hasQuote = (s) => /"/.test(s); const bn = <Banner title={err.code} />;\nconst e = err.envelope;\n'

    out = blank(src, strings=True, templates=True)

    assert "err.code" in out and "err.envelope" in out, out


@pytest.mark.parametrize("prefix", [") ", "+ ", "- ", "* ", "< ", "> ", ".", "% "], ids=lambda p: repr(p))
def test_the_positions_the_lexer_cannot_tell_stay_division(prefix: str) -> None:
    """After ``)`` (``if (x) /re/.test(y)``), ``+ - * %``, ``<`` / ``>`` (JSX) and ``.`` a ``/`` could be a division or a regex and the lexer takes it for a division: a quote inside a regex there
    opens a string that hides the rest of its line. The module docstring and ui-foundation.md name these positions; this pins that they are as described (review of #720, round 2, N2)."""
    out = blank(f'x = a {prefix}/"/.test(y); const e = err.envelope;\n', strings=True, templates=True)

    assert "err.envelope" not in out, (prefix, out)


def test_a_slash_inside_a_character_class_does_not_end_the_regex() -> None:
    out = blank("const r = /[/']+/g; const e = err.envelope;\n", strings=True, templates=True)

    assert "err.envelope" in out, out


# ---- a lexer that ends inside a construct says so ----------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    ["x = `abc", "x = `a ${b", "x = `a ${ `inner", "f('abc", 'f("abc'],
    ids=["a template", "a placeholder", "a nested template", "a single-quoted string at the end of the file", "a double-quoted string at the end of the file"],
)
def test_ending_inside_a_template_a_placeholder_or_a_string_raises(src: str) -> None:
    """The scans would otherwise read the rest of the file as if it were text and report fewer sites than there are (a one-file blindness the floors cannot see)."""
    with pytest.raises(ValueError, match="unterminated"):
        blank(src, strings=True, templates=True)


def test_the_string_raise_fires_only_when_the_text_ends_inside_the_string_itself() -> None:
    """A string ends at its newline (a JavaScript string cannot span lines; a quote in JSX text is a guess), so a file that ends with one and a trailing newline does not raise: the raise covers a text
    that stops mid-string, which is what a truncated read looks like (review of #720, round 2, N5)."""
    with pytest.raises(ValueError, match="unterminated"):
        blank("f('abc")
    blank("f('abc\n")


def test_a_string_that_ends_at_the_end_of_its_line_is_not_unterminated() -> None:
    """A quote in JSX text (``Don't``) opens a string that ends at the newline: the lexer is lenient there on purpose (see the module docstring)."""
    blank("<p>Don't save</p>\nconst e = err.envelope;\n", strings=True, templates=True)


def test_a_backslash_in_template_text_escapes_the_next_character() -> None:
    out = blank("x = `a\\`b ${c}`; y = err.envelope;", strings=True, templates=True)

    assert "err.envelope" in out and "c" in out.split("${")[1], out


def test_a_comment_inside_a_placeholder_is_a_comment() -> None:
    out = blank("x = `a ${ b /* ` */ + c } d`; y = err.envelope;", strings=True, templates=True)

    assert "err.envelope" in out and " + c " in out, out


def test_line_of_counts_from_one() -> None:
    assert [line_of("a\nb\nc", i) for i in (0, 2, 4)] == [1, 2, 3]
