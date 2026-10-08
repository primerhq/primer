"""``SH_callQuotesNotice``: a failed call is judged on the words it carries, read from where a failed call keeps them, and matched at the END (review of #575, round 2).

A failed ``invoke_agent`` answers ``{"type", "message": "subagent 'x' LLM stream failed: <the stream error's message>"}`` (``primer/toolset/system.py``), and the resume path
(``primer/worker/frames.py``) answers ``{"error": "subagent LLM stream failed: <the stream error's message>"}``: both END with the notice's own words. The console used to read
only ``.message`` and to accept the words ANYWHERE in the output, so a resumed call's notice was never promoted to the failure, and a short notice ("timeout") was promoted
for a call that failed for an unrelated reason that merely mentions it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
TURNS = (ROOT / "ui" / "foundation" / "shell-turns.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def quotes():
    ctx = mini_react_context("", TURNS)

    def go(output, message) -> bool:
        call = f"{{ payload: {{ output: {json.dumps(output)} }} }}"
        notice = f"{{ payload: {{ message: {json.dumps(message)} }} }}"
        return bool(ctx.eval(f"SH_callQuotesNotice({call}, {notice})"))

    try:
        yield go
    finally:
        ctx.close()


def _failed_invoke(agent: str, message: str) -> str:
    return json.dumps({"type": "provider-error", "message": f"subagent {agent!r} LLM stream failed: {message}"})


def test_a_failed_invoke_agent_quotes_the_notice_it_ended_on(quotes) -> None:
    assert quotes(_failed_invoke("grand", "the model fell over"), "the model fell over")


def test_the_resume_paths_error_body_quotes_it_too(quotes) -> None:
    """The worker's resume path answers ``{error}``, with no ``message`` key."""
    body = json.dumps({"error": "subagent LLM stream failed: the model fell over"})
    assert quotes(body, "the model fell over")


def test_a_plain_text_output_that_ends_with_the_words_quotes_them(quotes) -> None:
    assert quotes("subagent LLM stream failed: the model fell over", "the model fell over")


def test_the_words_inside_an_unrelated_failure_are_not_the_failure(quotes) -> None:
    """A short notice that merely occurs in the middle of another failure's text did not end that call."""
    body = json.dumps({"type": "bad-request", "message": "timeout while reading the file, the file was not read"})
    assert not quotes(body, "timeout")
    assert not quotes(json.dumps({"error": "the rate limit message was: slow down, then it retried"}), "slow down")


def test_the_words_must_end_the_text_at_a_word_boundary(quotes) -> None:
    """``error`` is not the end of ``terror``."""
    assert not quotes(json.dumps({"message": "subagent 'x' LLM stream failed: terror"}), "error")


def test_message_wins_over_error_when_a_body_carries_both(quotes) -> None:
    body = json.dumps({"message": "subagent 'x' LLM stream failed: boom", "error": "something else entirely"})
    assert quotes(body, "boom")
    assert not quotes(body, "entirely")


@pytest.mark.parametrize("output", [None, 7, {"message": "boom"}, ""])
def test_an_output_that_is_not_text_quotes_nothing(quotes, output) -> None:
    assert not quotes(output, "boom")


@pytest.mark.parametrize("message", [None, "", "   "])
def test_a_notice_without_words_is_never_quoted(quotes, message) -> None:
    assert not quotes(_failed_invoke("grand", "boom"), message)
