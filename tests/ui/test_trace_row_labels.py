"""A trace row says what its badge means and what the model call cost (console review C-017, 2026-10-08).

The trace pane drew its rows as ``T 12:32 AM workspace__ls 0s`` and ``A 12:32 AM operator 1s``: the badges were single letters with no legend or tooltip
(T for a tool call, A for a model call), and a model call's token counts, which the timeline node already carries (``input_tokens``, ``output_tokens``), were
nowhere. The badge is now named (``title`` and an accessible name) and a model call's row says its tokens, compactly, before its duration.

The real ``NV_traceGlyph``, ``NV_traceTokens`` and ``NV_TraceLine`` run in V8; both surfaces (the sidebar's one-liner and the maximize overlay's expandable row) draw the
badge through the one ``NV_TraceGlyph`` component, so they cannot drift, and ``tests/ui_e2e/test_trace_labels_journey.py`` reads both in a browser.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx").read_text(encoding="utf-8")
_NAMES = ("NV_traceElapsed", "NV_traceCompact", "NV_traceTokens", "NV_traceAgentName", "NV_traceRowLabel", "NV_traceGlyph", "NV_TraceGlyph", "NV_TraceLine")


def _function(name: str) -> str:
    start = DOC.index("function " + name + "(")
    return DOC[start:DOC.index("\n}\n", start) + len("\n}\n")]


@functools.lru_cache(maxsize=1)
def _compiled() -> str:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform("\n".join(_function(n) for n in _NAMES if ("function " + n + "(") in DOC), "snippet.jsx")
    finally:
        bundler._ctx.close()


# Every host element a render produced, with its props (the stand-in has no DOM to ask for a title or an accessible name).
_RECORDER = """
var ELS = [];
var __ce = React.createElement;
React.createElement = function (type, props) {
  var el = __ce.apply(null, arguments);
  if (typeof type === "string") ELS.push({ type: type, props: props || {} });
  return el;
};
"""


@pytest.fixture
def ctx():
    c = mini_react_context(_compiled(), (ROOT / "ui" / "foundation" / "shell-turns.js").read_text(encoding="utf-8") + _RECORDER)
    try:
        yield c
    finally:
        c.close()


def _eval(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.mark.parametrize(("tokens", "label"), [
    (0, "0"), (7, "7"), (950, "950"), (999, "999"), (1000, "1k"), (1234, "1.2k"), (9949, "9.9k"), (12345, "12k"), (999499, "999k"), (1234567, "1.2M"),
])
def test_token_counts_are_compact(ctx, tokens: int, label: str) -> None:
    assert _eval(ctx, f"NV_traceCompact({tokens})") == label


@pytest.mark.parametrize(("node", "text"), [
    ({"kind": "llm_call", "input_tokens": 1234, "output_tokens": 56}, "1.2k in · 56 out"),
    ({"kind": "llm_call", "input_tokens": 10, "output_tokens": 0}, "10 in · 0 out"),
    ({"kind": "llm_call", "input_tokens": 10, "output_tokens": None}, "10 in"),
    ({"kind": "llm_call", "input_tokens": None, "output_tokens": 5}, "5 out"),
    ({"kind": "llm_call", "input_tokens": None, "output_tokens": None}, ""),
    ({"kind": "llm_call"}, ""),
    ({"kind": "tool_call", "input_tokens": 99, "output_tokens": 99}, ""),
    ({"kind": "node"}, ""),
])
def test_a_model_calls_tokens_read_in_and_out_and_nothing_else_has_any(ctx, node: dict, text: str) -> None:
    assert _eval(ctx, f"NV_traceTokens({json.dumps(node)})") == text


def test_the_badges_are_named(ctx) -> None:
    assert _eval(ctx, 'NV_traceGlyph({kind: "tool_call"})') == {"char": "T", "kind": "tool", "title": "Tool call"}
    assert _eval(ctx, 'NV_traceGlyph({kind: "llm_call"})') == {"char": "A", "kind": "agent", "title": "Model call"}
    assert _eval(ctx, 'NV_traceGlyph({kind: "node"})') is None


def _line(ctx, node: dict) -> None:
    ctx.eval(f"MR.mount(NV_TraceLine, {{ node: {json.dumps(node)}, index: 0, depth: 0, agentName: 'operator', isGraph: false }});")


def _glyphs(ctx) -> list[dict]:
    return json.loads(ctx.eval(
        "JSON.stringify(ELS.filter(function (e) { return (e.props.className || '').split(' ').indexOf('nv-trace-glyph') >= 0; })"
        ".map(function (e) { return { role: e.props.role, title: e.props.title, label: e.props['aria-label'], kind: e.props['data-kind'] }; }))"
    ))


def test_a_model_call_row_names_its_badge_and_says_its_tokens(ctx) -> None:
    _line(ctx, {"kind": "llm_call", "ts": None, "duration_ms": 1000, "input_tokens": 1234, "output_tokens": 56})
    text = ctx.eval("MR.texts().join(' | ')")
    assert "1.2k in · 56 out" in text and "1s" in text and "operator" in text
    assert text.index("1.2k in") < text.index("1s"), "the tokens sit before the duration"
    glyph = _glyphs(ctx)
    assert glyph and glyph[-1] == {"role": "img", "title": "Model call", "label": "Model call", "kind": "agent"}, glyph


def test_a_tool_call_row_has_no_token_column(ctx) -> None:
    _line(ctx, {"kind": "tool_call", "ts": None, "duration_ms": 0, "name": "workspace__ls", "input_tokens": 5, "output_tokens": 5})
    text = ctx.eval("MR.texts().join(' | ')")
    assert "workspace__ls" in text and "5 in" not in text and "5 out" not in text
    assert _glyphs(ctx)[-1] == {"role": "img", "title": "Tool call", "label": "Tool call", "kind": "tool"}


def test_both_surfaces_draw_the_badge_and_the_tokens_through_the_shared_pieces() -> None:
    """The sidebar one-liner and the overlay's expandable row must not each carry their own copy of the badge or the tokens."""
    assert DOC.count("<NV_TraceGlyph") == 2
    assert DOC.count('<span className="nv-trace-tokens"') == 2
    assert 'className="nv-trace-glyph"' in _function("NV_TraceGlyph") and DOC.count('className="nv-trace-glyph"') == 1
