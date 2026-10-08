"""A subagent's failure and its recoverable problem are drawn in its own block (ticket 01a11c1e).

``NV_subagentRows`` drew a child row only when it carried a ``label``, and an error row has none (its words are in ``payload.message``), so a subagent that
failed showed nothing in its block. These run the REAL ``NV_subagentRows`` with the real ``NV_errorView`` and ``NV_noticeView`` in V8; the layout in a real
transcript, with a real delegated run, is checked by ``tests/ui_e2e/test_subagent_failure_journey.py``.
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

    def render(children: list[dict]):
        ctx = _context()
        made.append(ctx)
        ctx.eval("function Probe() { return NV_subagentRows({seq: 1, children: " + json.dumps(children) + "}, function () {}, false); } MR.mount(Probe, {});")
        return ctx

    try:
        yield render
    finally:
        for ctx in made:
            ctx.close()


_SUB = {"delegated": True, "delegate_run_id": "run-1", "delegate_tool_call_id": "call-1", "agent_id": "helper"}


def _error(seq: int, message: str = "boom", **extra) -> dict:
    return {"seq": seq, "kind": "error", "label": None, "payload": {"message": message, "code": "server_error", "fatal": True, **_SUB, **extra}}


def _notice(seq: int, state: str, message: str = "provider hiccup") -> dict:
    return {"seq": seq, "kind": "retry_notice", "label": None, "noticeState": state, "payload": {"message": message, "code": "x", "fatal": False, **_SUB}}


def _said(seq: int, text: str) -> dict:
    return {"seq": seq, "kind": "assistant_message", "label": text, "payload": {"agent_id": "helper", "delegated": True}}


def test_a_subagents_failure_is_a_red_card_in_its_block(block) -> None:
    ctx = block([_said(2, "working on it"), _error(3, "the model fell over")])
    assert ctx.eval('MR.find("nv-subagent-failure:3") !== null')
    text = ctx.eval("MR.texts().join(' | ')")
    assert "the model fell over" in text and "working on it" in text
    assert ctx.eval('MR.find("nv-subagent-failure:3").props.className').startswith("nv-subagent")


def test_the_failure_card_reads_like_the_main_transcripts_one(block) -> None:
    """Same words: the problem type or code in words, the provider's own text kept as the detail."""
    ctx = block([_error(3, "the model fell over", code="/errors/provider-server-error")])
    text = ctx.eval("MR.texts().join(' | ')")
    assert "The model provider had a server error." in text and "the model fell over" in text


def test_a_subagents_failure_names_the_agent(block) -> None:
    ctx = block([_error(3)])
    assert "helper" in ctx.eval("MR.texts().join(' | ')")


@pytest.mark.parametrize(("state", "words"), [
    ("retrying", "the turn is continuing"), ("recovered", "carried on"), ("ended", "before the turn ended"),
])
def test_a_subagents_notice_is_the_quiet_line_for_its_state(block, state: str, words: str) -> None:
    ctx = block([_notice(3, state)])
    assert ctx.eval('MR.find("nv-subagent-notice:3") !== null')
    text = ctx.eval("MR.texts().join(' | ')")
    assert words in text and "provider hiccup" in text


def test_neither_is_announced_as_an_alert(block) -> None:
    """A subagent's problem arrives inside a block the reader may not be looking at; the parent turn's own failure is what is announced."""
    ctx = block([_error(3), _notice(4, "ended")])
    assert ctx.eval('MR.find("nv-subagent-failure:3").props.role') in (None, "")
    assert ctx.eval('MR.find("nv-subagent-notice:4").props.role') in (None, "")


def test_a_labelled_child_and_a_row_with_nothing_to_say_behave_as_before(block) -> None:
    ctx = block([_said(2, "hello from the subagent"), {"seq": 5, "kind": "lifecycle", "label": "", "payload": {"delegated": True}}])
    text = ctx.eval("MR.texts().join(' | ')")
    assert "hello from the subagent" in text
    assert ctx.eval('MR.findAll("nv-subagent-failure:").length') == 0 and ctx.eval('MR.findAll("nv-subagent-notice:").length') == 0
