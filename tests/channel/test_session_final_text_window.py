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


def test_a_completed_turn_after_a_stop_with_no_text_relays_nothing_not_the_partial() -> None:
    records = [USER, _tok("partial"), {"kind": "cancelled", "payload": {}}, USER, _done()]

    assert derive_session_final_text(records) is None
