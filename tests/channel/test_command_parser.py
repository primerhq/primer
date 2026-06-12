"""Slash-command parser: /new /list /switch /agent."""

from __future__ import annotations

import pytest

from primer.channel.commands import ParsedCommand, help_text, parse_command


@pytest.mark.parametrize("text,verb,arg", [
    ("/new", "new", None),
    ("/list", "list", None),
    ("/switch chat-abc", "switch", "chat-abc"),
    ("/agent", "agent", None),
    ("/agent agent-7", "agent", "agent-7"),
    ("/help", "help", None),
    ("  /new  ", "new", None),
    ("/new@mybot", "new", None),
])
def test_parse_known(text, verb, arg):
    parsed = parse_command(text)
    assert parsed == ParsedCommand(verb=verb, arg=arg)


@pytest.mark.parametrize("text", ["hello", "", "  ", "/unknown", "not /new"])
def test_parse_non_command_returns_none(text):
    assert parse_command(text) is None


def test_help_text_single_type_includes_switch():
    txt = help_text(supports_threads=False)
    assert "/switch" in txt
    assert "/new" in txt and "/list" in txt
    assert "/agent" in txt and "/help" in txt


def test_help_text_multi_type_omits_switch():
    txt = help_text(supports_threads=True)
    assert "/switch" not in txt
    assert "/new" in txt and "/list" in txt
    assert "/agent" in txt and "/help" in txt
