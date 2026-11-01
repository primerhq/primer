"""Docstring to tool description. Enforced at registration, not call time."""

from __future__ import annotations

import pytest

from primer.toolset.python_runner.docstring import DocstringError, parse_docstring

GOOD = """Fetch a customer record by id.

    Use when you need a customer's billing details and have their id.

    Args:
        customer_id: The customer's opaque id.
        include_history: Whether to include past invoices.
    """


def test_summary_becomes_purpose() -> None:
    assert parse_docstring(GOOD).purpose == "Fetch a customer record by id."


def test_use_when_line_becomes_when() -> None:
    assert parse_docstring(GOOD).when.startswith("Use when")


def test_each_arg_is_captured() -> None:
    args = parse_docstring(GOOD).args
    assert args["customer_id"] == "The customer's opaque id."
    assert args["include_history"] == "Whether to include past invoices."


def test_a_multiline_arg_description_is_joined() -> None:
    parsed = parse_docstring(
        "Do a thing.\n\n"
        "    Use when you must.\n\n"
        "    Args:\n"
        "        a: first line\n"
        "            second line\n"
    )
    assert parsed.args["a"] == "first line second line"


def test_missing_docstring_is_an_error() -> None:
    with pytest.raises(DocstringError) as exc:
        parse_docstring("")
    assert exc.value.field == "docstring"


def test_missing_use_when_is_an_error_naming_the_field() -> None:
    with pytest.raises(DocstringError) as exc:
        parse_docstring("Fetch a thing.\n\n    Args:\n        a: x\n")
    assert exc.value.field == "when"


def test_a_when_section_is_accepted_instead_of_the_line() -> None:
    parsed = parse_docstring(
        "Fetch a thing.\n\n    When:\n        the cache is cold\n\n"
        "    Args:\n        a: x\n"
    )
    assert "cache is cold" in parsed.when


def test_examples_section_is_parsed_as_json() -> None:
    parsed = parse_docstring(
        "Fetch a thing.\n\n    Use when you must.\n\n"
        "    Args:\n        a: x\n\n"
        '    Examples:\n        {"a": "1"}\n'
    )
    assert parsed.examples == [{"a": "1"}]


def test_a_malformed_example_is_an_error() -> None:
    with pytest.raises(DocstringError) as exc:
        parse_docstring(
            "Fetch a thing.\n\n    Use when you must.\n\n"
            "    Args:\n        a: x\n\n"
            "    Examples:\n        not json\n"
        )
    assert exc.value.field == "examples"


def test_a_wrapped_arg_description_with_a_colon_is_kept_whole_in_a_cleaned_docstring() -> None:
    """GX-11: registration parses ``ast.get_docstring`` output, where names sit at 4 and continuations at 8; a continuation holding a colon
    (a URL, a time, "Note:") was read as a new argument."""
    parsed = parse_docstring(
        "Do a thing.\n\n"
        "Use when you must.\n\n"
        "Args:\n"
        "    url: the page to fetch, for example\n"
        "        https://example.com/a\n"
        "    when: a time such as\n"
        "        10:30\n"
    )
    assert parsed.args == {"url": "the page to fetch, for example https://example.com/a", "when": "a time such as 10:30"}


def test_a_wrapped_arg_description_with_a_colon_is_kept_whole_through_registration() -> None:
    from primer.toolset.python_runner.registration import register_module

    src = '''
@primer_tool()
def fetch(url: str) -> str:
    """Fetch one page.

    Use when the user names a page.

    Args:
        url: the page to fetch, for example
            https://example.com/a
    """
    return ""
'''
    [tool] = register_module(src, "ts", 30.0)
    assert tool.tool.args_schema["properties"]["url"]["description"] == "the page to fetch, for example https://example.com/a"
