"""A subagent's failure and its recoverable problem are drawn in its own block (ticket 01a11c1e).

``NV_subagentRows`` drew a child row only when it carried a ``label``, and an error row has none (its words are in ``payload.message``), so a subagent that
failed showed nothing in its block. These run the REAL ``NV_subagentRows`` with the real ``NV_errorView`` and ``NV_noticeView`` in V8; the layout in a real
transcript, with a real delegated run, is checked by ``tests/ui_e2e/test_subagent_failure_journey.py``.

The payloads are the recorder's real shape: ``DelegationRecorder`` stamps ``delegated``, ``delegate_tool_call_id`` and the run ids and NO ``agent_id``, so the
subagent's name comes from the delegating call (``payload.arguments.agent_id`` of ``system__invoke_agent``).

The only producer of a non-fatal Error (the OpenResponses stream) makes the agent loop hold the Error, yield the Done first and the Error last, and then raise,
so the delegating call is answered with an ERROR result and the delegated scope never gets a failure record of its own to absorb the notice. A notice whose
delegating call has failed is therefore drawn as the failure; one whose call has any result never says the turn is continuing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = DOC.index("function " + name + "(")
    return DOC[start:DOC.index("\n}\n", start) + len("\n}\n")]


def _context():
    from primer.api._jsx_bundle import JSXBundler

    source = "\n".join(_function(n) for n in ("NV_failureWords", "NV_errorView", "NV_noticeView", "NV_subagentRows"))
    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        code = bundler._transform(source, "snippet.jsx")
    finally:
        bundler._ctx.close()
    return mini_react_context(code, "function NV_toolCallWithRows() { return null; }")


@pytest.fixture
def block():
    made = []

    def render(children: list[dict], result: dict | None = None):
        """``result``: the delegating call's tool result (None while the call is still running)."""
        ctx = _context()
        made.append(ctx)
        call = {"seq": 1, "kind": "tool_call", "payload": {"name": "system__invoke_agent", "arguments": {"agent_id": "helper", "prompt": "do it"}}, "children": children}
        ctx.eval(
            "function Probe() { return NV_subagentRows(" + json.dumps(call) + ", function () { return " + json.dumps(result) + "; }, false); } MR.mount(Probe, {});"
        )
        return ctx

    try:
        yield render
    finally:
        for ctx in made:
            ctx.close()


_SUB = {"delegated": True, "delegate_run_id": "run-1", "delegate_tool_call_id": "call-1"}   # what DelegationRecorder stamps: no agent_id


def _error(seq: int, message: str = "boom", **extra) -> dict:
    return {"seq": seq, "kind": "error", "label": None, "payload": {"message": message, "code": "server_error", "fatal": True, **_SUB, **extra}}


def _notice(seq: int, state: str, message: str = "provider hiccup") -> dict:
    return {"seq": seq, "kind": "retry_notice", "label": None, "noticeState": state, "payload": {"message": message, "code": "x", "fatal": False, **_SUB}}


def _said(seq: int, text: str) -> dict:
    return {"seq": seq, "kind": "assistant_message", "label": text, "payload": dict(_SUB)}


def _result(*, error: bool) -> dict:
    return {"seq": 9, "kind": "tool_result", "createdAt": "t9", "payload": {"call_id": "call-1", "output": "subagent failed" if error else "done", "error": error}}


def _subtree(ctx, testid: str) -> list[dict]:
    return json.loads(ctx.eval(f'JSON.stringify(MR.subtree("{testid}"))'))


def _red_cards(ctx, testid: str) -> int:
    return sum(1 for el in _subtree(ctx, testid) if "nv-turn-error" in el["className"].split())


def _notes(ctx, testid: str) -> int:
    return sum(1 for el in _subtree(ctx, testid) if "nv-turn-note" in el["className"].split())


def test_a_subagents_failure_is_a_red_card_in_its_block(block) -> None:
    ctx = block([_said(2, "working on it"), _error(3, "the model fell over")])
    text = ctx.eval("MR.texts().join(' | ')")
    assert "the model fell over" in text and "working on it" in text
    assert _red_cards(ctx, "nv-subagent-failure:3") == 1, "the red card is INSIDE the failure's box, not only named by its outer class"
    assert _notes(ctx, "nv-subagent-failure:3") == 0


def test_the_failure_card_reads_like_the_main_transcripts_one(block) -> None:
    """Same words: the problem type or code in words, the provider's own text kept as the detail."""
    ctx = block([_error(3, "the model fell over", code="/errors/provider-server-error")])
    text = ctx.eval("MR.texts().join(' | ')")
    assert "The model provider had a server error." in text and "the model fell over" in text


def test_the_agents_name_comes_from_the_delegating_call_because_the_recorder_stamps_none(block) -> None:
    ctx = block([_said(2, "hello"), _error(3), _notice(4, "ended")])
    heads = [el for el in json.loads(ctx.eval("JSON.stringify(MR.findAll('nv-subagent-').map(function (e) { return e.props['data-testid']; }))"))]
    assert heads == ["nv-subagent-failure:3", "nv-subagent-notice:4"]
    text = ctx.eval("MR.texts().join(' | ')")
    assert text.count("helper") == 3, "every child of the call is under the agent's name"
    assert "subagent" not in text.replace("subagent failed", ""), "no child falls back to the bare label while the call names its agent"


@pytest.mark.parametrize(("state", "words"), [
    ("retrying", "the turn is continuing"), ("recovered", "carried on"), ("ended", "before the turn ended"),
])
def test_a_subagents_notice_is_the_quiet_line_for_its_state_while_the_call_is_still_running(block, state: str, words: str) -> None:
    ctx = block([_notice(3, state)], result=None)
    assert ctx.eval('MR.find("nv-subagent-notice:3") !== null')
    text = ctx.eval("MR.texts().join(' | ')")
    assert words in text and "provider hiccup" in text
    assert _notes(ctx, "nv-subagent-notice:3") == 1 and _red_cards(ctx, "nv-subagent-notice:3") == 0


@pytest.mark.parametrize("state", ["retrying", "recovered", "ended"])
def test_a_notice_of_a_call_that_failed_is_drawn_as_the_failure(block, state: str) -> None:
    """The loop holds a non-fatal Error and raises when the stream ends, so the call answers with an error: the notice IS the failure, in its own words, and
    it is the block's one red card (there is no fatal record of the delegated scope for it to be absorbed by)."""
    ctx = block([_notice(3, state, "provider hiccup")], result=_result(error=True))
    assert ctx.eval('MR.find("nv-subagent-notice:3") === null')
    assert ctx.eval('MR.find("nv-subagent-failure:3") !== null')
    assert _red_cards(ctx, "nv-subagent-failure:3") == 1
    text = ctx.eval("MR.texts().join(' | ')")
    assert "provider hiccup" in text and "continuing" not in text


def test_a_notice_of_a_call_that_succeeded_never_says_the_turn_is_continuing(block) -> None:
    ctx = block([_notice(3, "retrying")], result=_result(error=False))
    assert ctx.eval('MR.find("nv-subagent-notice:3") !== null'), "the call did not fail, so it stays the quiet line"
    text = ctx.eval("MR.texts().join(' | ')")
    assert "continuing" not in text and "carried on" in text


def test_neither_is_announced_as_an_alert_anywhere_in_its_subtree(block) -> None:
    """A subagent's problem arrives inside a block the reader may not be looking at; the parent turn's own failure is what is announced."""
    ctx = block([_error(3), _notice(4, "ended")])
    for testid in ("nv-subagent-failure:3", "nv-subagent-notice:4"):
        nodes = _subtree(ctx, testid)
        assert nodes, testid
        assert [n for n in nodes if n["role"] is not None or n["ariaLive"] is not None] == [], (testid, nodes)
    failed = block([_notice(5, "retrying")], result=_result(error=True))
    assert [n for n in _subtree(failed, "nv-subagent-failure:5") if n["role"] is not None or n["ariaLive"] is not None] == []


def test_a_labelled_child_and_a_row_with_nothing_to_say_behave_as_before(block) -> None:
    ctx = block([_said(2, "hello from the subagent"), {"seq": 5, "kind": "lifecycle", "label": "", "payload": dict(_SUB)}])
    text = ctx.eval("MR.texts().join(' | ')")
    assert "hello from the subagent" in text
    assert ctx.eval('MR.findAll("nv-subagent-failure:").length') == 0 and ctx.eval('MR.findAll("nv-subagent-notice:").length') == 0
