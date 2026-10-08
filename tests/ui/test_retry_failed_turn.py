"""A failed turn can be sent again from its error card (console review C-024, 2026-10-08).

A model call that answered HTTP 500 ended the session after its retries and left one error card and the line "Send a message to try again": the
operator had to retype or copy the instruction that had just failed. The card now offers Retry when that is a sound thing to do, and
``SH_retryInstruction`` (``ui/foundation/shell-turns.js``, pure) decides it: the text of the user message that opened the turn, or ``null`` when
a resend would be wrong or impossible:

* the failure is not the last content of the transcript (the operator already sent something after it, or an answer follows);
* the session is running (a turn is in flight, a second send would queue behind it);
* there is no user message before the failure, or it carried attachments (a resend of the text alone is not the same instruction);
* the row is not an error at all.

The decision runs in V8 against the real module; ``tests/ui_e2e/test_retry_failed_turn_journey.py`` drives the real console.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-turns.js"
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


@pytest.fixture
def ctx():
    from py_mini_racer import MiniRacer

    c = MiniRacer()
    c.eval("var window = globalThis;")
    c.eval((ROOT / "ui" / "foundation" / "shell-status.js").read_text(encoding="utf-8"))
    c.eval((ROOT / "ui" / "components" / "session-adapter.jsx").read_text(encoding="utf-8"))
    c.eval(MODULE.read_text(encoding="utf-8"))
    try:
        yield c
    finally:
        c.close()


def _retry(ctx, flat: list[dict], error_seq: int, session: dict | None = None):
    session = {"session_state": "ended"} if session is None else session
    return json.loads(ctx.eval(
        "JSON.stringify(SH_retryInstruction("
        f"{json.dumps(flat)}, {json.dumps(flat)}.filter(function (r) {{ return r.seq === {error_seq}; }})[0], {json.dumps(session)}))"
    ))


def _user(seq: int, text: str = "please do the thing", **payload) -> dict:
    return {"seq": seq, "kind": "user_message", "label": text, "payload": payload}


def _error(seq: int) -> dict:
    return {"seq": seq, "kind": "error", "label": "OpenAI server error", "payload": {"code": "/errors/provider-server-error"}}


def test_the_instruction_that_opened_the_failed_turn_is_offered(ctx) -> None:
    assert _retry(ctx, [_user(1), _error(2)], 2) == "please do the thing"


def test_markers_after_the_failure_do_not_hide_it(ctx) -> None:
    """The dispatch writes a bare release marker and the end divider after the error; neither is content."""
    flat = [_user(1), _error(2), {"seq": 3, "kind": "done", "label": "", "payload": {}}, {"seq": 4, "kind": "divider", "label": "session ended", "payload": {}}]
    assert _retry(ctx, flat, 2) == "please do the thing"


def test_the_nearest_user_message_is_the_instruction(ctx) -> None:
    flat = [_user(1, "first"), {"seq": 2, "kind": "assistant_message", "label": "ok", "payload": {}}, _user(3, "second"), _error(4)]
    assert _retry(ctx, flat, 4) == "second"


@pytest.mark.parametrize("kind", ["user_message", "assistant_message", "tool_call"])
def test_nothing_is_offered_when_content_follows_the_failure(ctx, kind: str) -> None:
    flat = [_user(1), _error(2), {"seq": 3, "kind": kind, "label": "later", "payload": {}}]
    assert _retry(ctx, flat, 2) is None


def test_nothing_is_offered_while_the_session_is_running(ctx) -> None:
    assert _retry(ctx, [_user(1), _error(2)], 2, {"session_state": "running"}) is None


def test_nothing_is_offered_without_a_session_row(ctx) -> None:
    assert json.loads(ctx.eval(
        "JSON.stringify(SH_retryInstruction([{seq: 1, kind: 'user_message', label: 'x', payload: {}}, {seq: 2, kind: 'error', label: 'e', payload: {}}],"
        " {seq: 2, kind: 'error', label: 'e', payload: {}}, null))"
    )) is None


def test_nothing_is_offered_with_no_user_message_before_the_failure(ctx) -> None:
    assert _retry(ctx, [{"seq": 1, "kind": "assistant_message", "label": "hi", "payload": {}}, _error(2)], 2) is None


@pytest.mark.parametrize("payload", [{"attachments": [{"path": "a.png"}]}, {"parts": [{"type": "image", "path": "a.png"}]}])
def test_nothing_is_offered_when_the_instruction_carried_attachments(ctx, payload: dict) -> None:
    assert _retry(ctx, [_user(1, **payload), _error(2)], 2) is None


def test_a_blank_instruction_is_not_resent(ctx) -> None:
    assert _retry(ctx, [_user(1, "   "), _error(2)], 2) is None


def test_a_row_that_is_not_an_error_has_nothing_to_retry(ctx) -> None:
    assert _retry(ctx, [_user(1), {"seq": 2, "kind": "done", "label": "", "payload": {}}], 2) is None


def test_the_error_card_wires_retry_only_for_the_session_own_turn() -> None:
    """A subagent's failure (nested, depth > 0) is not the session's instruction to resend."""
    card = DOC[DOC.index('if (row.kind === "error") {'):DOC.index("// A lifecycle row with nothing to say renders nothing at all.")]
    assert "depth ? null : SH_retryInstruction(flat, row, session)" in card
    assert 'data-testid={"nv-turn-retry:" + row.seq}' in card
    assert "retryFailedTurn(" in card
