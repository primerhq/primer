"""Begin firing shapes NodeOutput.text/parsed/history from initial_input."""

from __future__ import annotations

import json

import pytest

from primer.graph.base import _materialise_begin_output
from primer.model.chat import Message, TextPart


def test_dict_input_populates_parsed_and_text_is_json() -> None:
    out = _materialise_begin_output({"q": "hello"}, [])
    assert out.parsed == {"q": "hello"}
    assert json.loads(out.text) == {"q": "hello"}
    assert out.iteration == 0


def test_string_input_populates_text_only() -> None:
    out = _materialise_begin_output(
        "research X",
        [Message(role="user", parts=[TextPart(text="research X")])],
    )
    assert out.parsed is None
    assert "research X" in out.text


def test_message_list_input_concatenates_text() -> None:
    msgs = [
        Message(role="user", parts=[TextPart(text="part 1")]),
        Message(role="user", parts=[TextPart(text="part 2")]),
    ]
    out = _materialise_begin_output(None, msgs)
    assert out.parsed is None
    assert "part 1" in out.text and "part 2" in out.text
    assert out.history == msgs
