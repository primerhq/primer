"""``mask_values`` masks configured secret values in a failure text, in every form the text can show them (#672 review round 1).

It is the toolset-level sibling of ``scrub`` (which knows a model provider's key and Base URL password): an MCP toolset has HEADER values and no provider
object, so the forms logic is shared and the values are handed in. The nits of the same review are pinned here too: a value holding BOTH quote kinds is
shown by a Python ``repr`` with one of them backslash-escaped and by JSON with the double quote escaped, and the keyless-placeholder set is compared with
the NORMALISED value (``" none\\n"`` is the placeholder ``none``, and blanking the word would corrupt ordinary prose).
"""

from __future__ import annotations

import json

KEY = "sk-live-Q7xZ9pL2mN4vB8kR1tY6wE3"
BOTH_QUOTES = "a\"b'c-" + KEY


def mask_values(text, values):
    """Imported when called: before the fix the name does not exist, and a failed import would abort the collection of every test in the file."""
    from primer.llm._failure import mask_values as real

    return real(text, values)


def test_the_value_is_masked_whole_and_in_its_escaped_and_normalised_forms() -> None:
    padded = KEY + "\n"
    text = f"Illegal header value b'Bearer {padded!r}' / {KEY} / Bearer {KEY.encode('unicode_escape').decode()}"

    out = mask_values(text, [f"Bearer {padded}", padded])

    assert KEY not in out and "Q7xZ9pL2mN4vB8kR1tY6wE3" not in out


def test_a_value_with_both_quote_kinds_is_masked_in_its_repr_and_json_forms() -> None:
    for shown in (repr(BOTH_QUOTES), json.dumps(BOTH_QUOTES), repr(repr(BOTH_QUOTES)), BOTH_QUOTES):
        out = mask_values(f"upstream said {shown} was refused", [BOTH_QUOTES])
        assert "Q7xZ9pL2mN4vB8kR1tY6wE3" not in out, shown
        assert "[REDACTED]" in out, shown


def test_a_value_with_an_internal_newline_is_masked_in_its_escaped_form() -> None:
    value = "sk-live-Q7xZ9pL2mN4v\nB8kR1tY6wE3"

    out = mask_values(f"Illegal header value {value!r}", [value])

    assert "B8kR1tY6wE3" not in out and "sk-live-Q7xZ9pL2mN4v" not in out


def test_a_text_without_the_value_comes_back_unchanged() -> None:
    text = "could not connect to the MCP server: All connection attempts failed"

    assert mask_values(text, [KEY]) == text


def test_a_short_value_is_masked_as_a_token_only_and_a_tiny_one_not_at_all() -> None:
    assert mask_values("key abcd1234 and abcd", ["abcd"]) == "key abcd1234 and [REDACTED]"
    assert mask_values("abc is fine", ["abc"]) == "abc is fine"


def test_a_keyless_placeholder_is_never_a_secret_whatever_its_padding() -> None:
    """``none`` is what a keyless local server is configured with; the same word with whitespace around it is the same placeholder."""
    text = "none of the results matched"

    for value in ("none", "none\n", " NONE ", "\tdummy\r\n"):
        assert mask_values(text, [value]) == text, repr(value)
