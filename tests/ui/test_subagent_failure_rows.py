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

import functools
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = DOC.index("function " + name + "(")
    return DOC[start:DOC.index("\n}\n", start) + len("\n}\n")]


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    """The snippet, transpiled once for the module (the Babel bundler takes seconds to start)."""
    from primer.api._jsx_bundle import JSXBundler

    source = "\n".join(_function(n) for n in ("NV_failureWords", "NV_errorView", "NV_noticeView", "NV_subagentRows"))
    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        code = bundler._transform(source, "snippet.jsx")
    finally:
        bundler._ctx.close()
    return code


def _context():
    turns = (ROOT / "ui" / "foundation" / "shell-turns.js").read_text(encoding="utf-8")   # NV_subagentRows asks it which notice the failed call quotes
    return mini_react_context(_compiled(), "function NV_toolCallWithRows() { return null; }\n" + turns)


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


def _failed_output(message: str) -> str:
    """What a failed ``invoke_agent`` answers (``_err``: a ``{type, message}`` JSON text): it QUOTES the stream error the subagent ended on."""
    return json.dumps({"type": "provider-error", "message": f"subagent 'helper' LLM stream failed: {message}"})


def _result(*, error: bool, quoting: str = "provider hiccup") -> dict:
    output = _failed_output(quoting) if error else json.dumps({"output": "done"})
    return {"seq": 9, "kind": "tool_result", "createdAt": "t9", "payload": {"call_id": "call-1", "output": output, "error": error}}


def _subtree(ctx, testid: str) -> list[dict]:
    nodes = json.loads(ctx.eval(f'JSON.stringify(MR.subtree("{testid}"))'))
    assert nodes, f"nothing is drawn inside {testid}: a walk over it would pass on an empty box"
    return nodes


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
    boxes = json.loads(ctx.eval(
        "JSON.stringify(MR.findAll('nv-subagent-failure:').concat(MR.findAll('nv-subagent-notice:')).map(function (e) { return e.props['data-testid']; }))"
    ))
    assert boxes == ["nv-subagent-failure:3", "nv-subagent-notice:4"]
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
        assert [n for n in nodes if n.get("role") is not None or n.get("ariaLive") is not None] == [], (testid, nodes)
    failed = block([_notice(5, "retrying")], result=_result(error=True))
    assert [n for n in _subtree(failed, "nv-subagent-failure:5") if n.get("role") is not None or n.get("ariaLive") is not None] == []


def test_a_labelled_child_and_a_row_with_nothing_to_say_behave_as_before(block) -> None:
    ctx = block([_said(2, "hello from the subagent"), {"seq": 5, "kind": "lifecycle", "label": "", "payload": dict(_SUB)}])
    text = ctx.eval("MR.texts().join(' | ')")
    assert "hello from the subagent" in text
    assert ctx.eval('MR.findAll("nv-subagent-failure:").length') == 0 and ctx.eval('MR.findAll("nv-subagent-notice:").length') == 0


def test_only_the_notice_the_failed_calls_error_quotes_is_the_failure(block) -> None:
    """Two notices in one failed call's scope are two provider reports; the call's error output quotes the one it ended on, and that is the red card. The other stays
    the quiet line (two red cards would say the call failed twice)."""
    ctx = block([_notice(3, "recovered", "first hiccup"), _notice(4, "ended", "second hiccup")], result=_result(error=True, quoting="second hiccup"))
    assert ctx.eval('MR.find("nv-subagent-failure:4") !== null') and ctx.eval('MR.find("nv-subagent-notice:4") === null')
    assert ctx.eval('MR.find("nv-subagent-notice:3") !== null') and ctx.eval('MR.find("nv-subagent-failure:3") === null')
    assert _red_cards(ctx, "nv-subagent-failure:4") == 1 and _notes(ctx, "nv-subagent-notice:3") == 1


def test_a_failed_call_that_quotes_no_notice_promotes_none(block) -> None:
    """The call failed for a reason the notice does not name (a tool error, a depth limit): the notice keeps its own state's words instead of being called the failure."""
    ctx = block([_notice(3, "ended", "provider hiccup")], result=_result(error=True, quoting="something else entirely"))
    assert ctx.eval('MR.find("nv-subagent-notice:3") !== null') and ctx.eval('MR.find("nv-subagent-failure:3") === null')
    assert "continuing" not in ctx.eval("MR.texts().join(' | ')")


def test_the_same_words_twice_still_make_one_red_card(block) -> None:
    ctx = block([_notice(3, "recovered", "provider hiccup"), _notice(4, "ended", "provider hiccup")], result=_result(error=True))
    assert len(json.loads(ctx.eval('JSON.stringify(MR.findAll("nv-subagent-failure:").map(function (e) { return e.props["data-testid"]; }))'))) == 1
    assert ctx.eval('MR.find("nv-subagent-failure:4") !== null'), "the last one the output quotes is the call's failure"
