"""``derive_session_final_text``: which text a turn relays to the channel.

The relayed text is the joined ``assistant_token`` text of the LAST completed turn: the rows between
the previous terminal record and the final ``done``. A turn that was STOPPED (or failed) has no
``done``, only a ``cancelled`` (or ``error``) record, and the partial text it streamed is now written
as ``assistant_token`` records before it (``flush_partial_output``). If only ``done`` bounded the
window, that partial text would be joined onto the NEXT turn's reply:
"Here is a long partial ansShort reply."
"""

from __future__ import annotations

from primer.channel.session_relay import derive_session_final_text


def _tok(text: str) -> dict:
    return {"kind": "assistant_token", "payload": {"text": text}}


def _done(stop_reason: str = "stop") -> dict:
    return {"kind": "done", "payload": {"stop_reason": stop_reason}}


USER = {"kind": "user_input", "payload": {"text": "hi"}}


def test_a_completed_turn_relays_its_text() -> None:
    assert derive_session_final_text([USER, _tok("Hello"), _tok(" there"), _done()]) == "Hello there"


def test_a_stopped_turn_does_not_leak_its_partial_text_into_the_next_reply() -> None:
    records = [
        USER, _tok("Here is a long partial ans"), {"kind": "cancelled", "payload": {"reason": "operator_interrupt"}},
        USER, _tok("Short reply."), _done(),
    ]

    assert derive_session_final_text(records) == "Short reply."


def test_a_failed_turn_does_not_leak_its_partial_text_either() -> None:
    records = [
        USER, _tok("half an ans"), {"kind": "error", "payload": {"message": "provider failed"}},
        USER, _tok("Second try."), _done(),
    ]

    assert derive_session_final_text(records) == "Second try."


def test_only_the_last_round_of_a_tool_turn_is_relayed_as_before() -> None:
    records = [USER, _tok("Let me check."), _done("tool_use"), _tok("The answer is 4."), _done()]

    assert derive_session_final_text(records) == "The answer is 4."


def test_a_stopped_turn_with_no_completed_turn_after_it_relays_nothing() -> None:
    records = [USER, _tok("partial"), {"kind": "cancelled", "payload": {"reason": "operator_interrupt"}}]

    assert derive_session_final_text(records) is None


CANCELLED = {"kind": "cancelled", "payload": {"reason": "operator_cancel"}}
ERROR = {"kind": "error", "payload": {"message": "provider failed"}}


def test_a_turn_cancelled_after_its_done_relays_nothing() -> None:
    """A Cancel that lands after the model's terminal event ends the session as cancelled, and the transcript
    reads ``tokens, done, cancelled``. The answer is in the log, but the user cancelled: it must not be handed
    to the webhook hold (or any reader of the final text) as the run's result."""
    assert derive_session_final_text([USER, _tok("The full answer."), _done(), CANCELLED]) is None


def test_a_stopped_turn_after_a_completed_one_does_not_relay_the_earlier_answer() -> None:
    """The latest turn did not complete, so there is no final text: the PREVIOUS turn's answer is not it."""
    records = [USER, _tok("Old answer."), _done(), USER, _tok("partial"), CANCELLED]

    assert derive_session_final_text(records) is None


def test_a_failure_after_the_done_relays_nothing() -> None:
    assert derive_session_final_text([USER, _tok("The answer."), _done(), ERROR]) is None


def test_a_log_with_no_terminal_record_has_no_final_text() -> None:
    """An empty log, and a turn that is still streaming (tokens, no done yet), have not completed a turn."""
    assert derive_session_final_text([]) is None
    assert derive_session_final_text([USER, _tok("still typing")]) is None


def test_a_turn_that_completes_after_an_earlier_cancel_still_relays() -> None:
    records = [USER, _tok("cut off"), CANCELLED, USER, _tok("Here you go."), _done()]

    assert derive_session_final_text(records) == "Here you go."


async def test_the_reader_applies_the_same_rule_to_a_late_cancelled_log() -> None:
    import json

    from primer.channel.session_relay import read_session_final_text

    class _Io:
        def read_lines(self, session_id: str) -> list[str]:
            return [json.dumps(r) for r in [USER, _tok("The full answer."), _done(), CANCELLED]]

    assert await read_session_final_text(_Io(), "s1") is None


def test_a_completed_turn_after_a_stop_with_no_text_relays_nothing_not_the_partial() -> None:
    records = [USER, _tok("partial"), {"kind": "cancelled", "payload": {}}, USER, _done()]

    assert derive_session_final_text(records) is None


def test_a_failed_turn_that_ends_in_a_done_with_stop_reason_error_relays_nothing() -> None:
    """A turn that fails mid-stream can finish with ``Done(stop_reason="error")`` and no ``error`` record before it.
    That ``done`` is the turn's last terminal record, but the turn did not complete: its partial text is not a result."""
    records = [USER, _tok("half an ans"), _done("error")]

    assert derive_session_final_text(records) is None


def test_a_done_with_stop_reason_error_after_an_error_record_relays_nothing() -> None:
    records = [USER, _tok("half an ans"), {"kind": "error", "payload": {"message": "provider failed"}}, _done("error")]

    assert derive_session_final_text(records) is None


def test_a_failed_turn_does_not_hide_the_next_turns_reply() -> None:
    records = [USER, _tok("half an ans"), _done("error"), USER, _tok("Second try."), _done()]

    assert derive_session_final_text(records) == "Second try."


def test_a_reply_cut_off_by_the_token_limit_is_still_relayed() -> None:
    """``max_tokens`` is a truncated answer, not a failure: it is the turn's result and the session rests WAITING."""
    records = [USER, _tok("a long answer that was cut"), _done("max_tokens")]

    assert derive_session_final_text(records) == "a long answer that was cut"
