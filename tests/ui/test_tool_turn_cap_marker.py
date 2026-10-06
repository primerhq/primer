"""The console shows the interactive tool-turn-cap marker (ticket 01a1095e, sub-point 2).

A turn that stops at ``max_tool_turns`` now ends on a ``done`` record whose ``stop_reason`` is ``tool_turn_cap``
(``tests/agent/test_tool_turn_cap_marker.py``). Before, that record was the model's own ``tool_use`` done, which the
transcript hides (a ``tool_use`` done ends one model call, not the turn), so an interactive session that hit the cap
rested like any other and the operator saw nothing. The marker must be a visible row, labelled for what it is, and the
real ``tool_use`` rounds must stay hidden. The pure decisions are driven through MiniRacer against the real sources.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL_STATUS = ROOT / "ui" / "foundation" / "shell-status.js"
ADAPTER = ROOT / "ui" / "components" / "session-adapter.jsx"
FRAME = ROOT / "ui" / "components" / "session-frame.jsx"

# Every V8 isolate this file creates, closed after each test (an undisposed isolate lives until the process ends).
_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _ctx(*sources: Path):
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = globalThis;")
    for src in sources:
        ctx.eval(src.read_text(encoding="utf-8"))
    return ctx


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def test_the_marker_reads_as_a_stop_at_the_cap_and_an_ordinary_done_keeps_its_label() -> None:
    ctx = _ctx(SHELL_STATUS)

    assert _js(ctx, 'SH_lifecycleLabel("done", {stop_reason: "tool_turn_cap"})') == "■ stopped at the tool-turn cap"
    assert _js(ctx, 'SH_lifecycleLabel("done", {stop_reason: "stop"})') == "· done"
    assert _js(ctx, 'SH_lifecycleLabel("done", {})') == "· done"
    assert _js(ctx, 'SH_lifecycleLabel("done", null)') == "· done"


def test_the_transcript_keeps_the_marker_row_and_still_hides_a_tool_round_done() -> None:
    ctx = _ctx(ADAPTER)
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "user_input", payload: {text: "go"}, created_at: "t1", node_id: null},
          {seq: 2, kind: "done", payload: {stop_reason: "tool_use", raw_reason: "tool_use"}, created_at: "t2", node_id: null},
          {seq: 3, kind: "done", payload: {stop_reason: "tool_turn_cap", raw_reason: "tool_use"}, created_at: "t3", node_id: null},
        ];
        var out = window.SA_toTranscript(records, {id: "s1"});
        """
    )

    assert _js(ctx, "out.map(function (r) { return [r.seq, r.kind]; })") == [[1, "user_message"], [3, "done"]]
    assert _js(ctx, "out[1].payload.stop_reason") == "tool_turn_cap"


def test_the_live_frame_row_does_not_paint_the_marker_as_a_success() -> None:
    """The stream frame (graph node inspector, live view) printed ``done (<reason>)`` in green for every done."""
    src = FRAME.read_text(encoding="utf-8")

    assert "tool_turn_cap" in src, "session-frame.jsx has no branch for the capped done"
    assert 'stopReason === "tool_turn_cap"' in src
