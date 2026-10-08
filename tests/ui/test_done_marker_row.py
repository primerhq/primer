"""A clean turn end does not print the record's name in the transcript (console review C-016, 2026-10-08).

Every finished turn ended in a faint "· done": the name of an internal record (``done``), in a transcript that is otherwise prose, for a fact the
status chip and the answer itself already state. The row is also where the turn's trace button lives (it IS the turn boundary), so the row stays
and the word goes: a clean end draws the trace button alone, and an end that is NOT clean says why in words, because there the marker is news
(the output limit cut the answer off, the content filter blocked it, the model call failed, the tool-turn cap stopped the work).

``SH_lifecycleLabel`` is pure; ``NV_LifecycleRow`` (the row itself) runs in V8 on the hook runtime in ``tests/ui/_mini_react.py``.

Follow-ups to #594: a clean done draws the icon-only trace button alone, and a ``title`` on a button with no text of its own is not an accessible name,
so the button carries an ``aria-label`` beside it; ``other`` is a real ``StopReason`` (``primer/model/chat.py``) that providers with many finish reasons
collapse into, so an unclean done that ends for another reason now says so in words instead of reading as clean; and the clean-end pin is no longer only
"no words" - it also asserts that no ``nv-lifecycle-dot`` element is drawn at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
SHELL_STATUS = ROOT / "ui" / "foundation" / "shell-status.js"
DOC = ROOT / "ui" / "components" / "console" / "nv-session-doc.jsx"


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(DOC)


@pytest.fixture
def ctx(code):
    c = mini_react_context(code, SHELL_STATUS.read_text(encoding="utf-8") + "\nvar TRACED = [];")
    try:
        yield c
    finally:
        c.close()


def _label(ctx, kind: str, payload) -> str:
    return json.loads(ctx.eval(f"JSON.stringify(SH_lifecycleLabel({json.dumps(kind)}, {json.dumps(payload)}))"))


@pytest.mark.parametrize("payload", [{}, None, {"stop_reason": "stop"}, {"stop_reason": "stop_sequence"}])
def test_a_clean_done_has_no_label(ctx, payload) -> None:
    assert _label(ctx, "done", payload) == ""


@pytest.mark.parametrize(
    ("reason", "label"),
    [
        ("max_tokens", "■ cut off at the output limit"),
        ("content_filter", "■ blocked by the content filter"),
        ("error", "■ ended with an error"),
        ("tool_turn_cap", "■ stopped at the tool-turn cap"),
        ("other", "■ ended for another reason"),
    ],
)
def test_a_done_that_is_not_clean_says_why(ctx, reason: str, label: str) -> None:
    assert _label(ctx, "done", {"stop_reason": reason}) == label


def test_the_other_markers_keep_their_labels(ctx) -> None:
    assert _label(ctx, "yielded", {}) == "· yielded"
    assert _label(ctx, "resumed", {}) == "· resumed"
    assert _label(ctx, "cancelled", {"reason": "operator_interrupt"}) == "■ stopped"
    assert _label(ctx, "cancelled", None) == "■ cancelled"


def _mount(ctx, kind: str, payload: dict | None = None, seq: int = 7) -> None:
    row = {"seq": seq, "kind": kind, "payload": payload or {}}
    ctx.eval(f"MR.mount(NV_LifecycleRow, {{ row: {json.dumps(row)}, onTrace: function (r) {{ TRACED.push(r.seq); }} }});")


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def test_a_clean_done_draws_the_trace_button_and_no_words(ctx) -> None:
    _mount(ctx, "done", {"stop_reason": "stop"})
    assert ctx.eval("MR.find('nv-turn:7') !== null")
    assert ctx.eval("MR.find('nv-trace-open:7') !== null"), "the row is the turn boundary and carries the trace button"
    assert _texts(ctx).strip() == ""
    assert "done" not in _texts(ctx)


def test_the_trace_button_opens_that_rows_turn(ctx) -> None:
    _mount(ctx, "done")
    ctx.eval("MR.click('nv-trace-open:7');")
    assert json.loads(ctx.eval("JSON.stringify(TRACED)")) == [7]


@pytest.mark.parametrize("kind", ["done", "cancelled"])
def test_a_trace_button_is_on_a_done_or_cancelled_row_only(ctx, kind: str) -> None:
    _mount(ctx, kind)
    assert ctx.eval("MR.find('nv-trace-open:7') !== null")


@pytest.mark.parametrize("kind", ["yielded", "resumed"])
def test_a_yield_or_resume_row_has_its_word_and_no_trace_button(ctx, kind: str) -> None:
    _mount(ctx, kind)
    assert ctx.eval("MR.find('nv-trace-open:7') === null")
    assert f"· {kind}" in _texts(ctx)


def test_an_unclean_done_shows_its_reason_beside_the_trace_button(ctx) -> None:
    _mount(ctx, "done", {"stop_reason": "max_tokens"})
    assert "cut off at the output limit" in _texts(ctx)
    assert ctx.eval("MR.find('nv-trace-open:7') !== null")


def test_a_stopped_turn_still_reads_stopped(ctx) -> None:
    _mount(ctx, "cancelled", {"reason": "operator_interrupt"})
    assert "■ stopped" in _texts(ctx)


def _trace_button_props(ctx) -> dict:
    return json.loads(ctx.eval("JSON.stringify((function () { var b = MR.find('nv-trace-open:7'); return b && b.props || null; })())"))


def test_the_icon_only_trace_button_carries_an_accessible_name(ctx) -> None:
    """A clean done draws the button alone: a title on a button with no text of its own is not an accessible name, so the button has an aria-label."""
    _mount(ctx, "done", {"stop_reason": "stop"})

    props = _trace_button_props(ctx)
    assert props is not None
    assert props["aria-label"] == "View trace for this turn"
    assert props["title"] == "View trace", "the visible hint stays"


def _dot_children(ctx) -> int:
    return json.loads(ctx.eval(
        "JSON.stringify((function () { var el = MR.find('nv-turn:7'); var kids = el && el.children || []; return kids.filter(function (c) {"
        " return c && c.__el && c.type === 'span' && c.props && c.props.className === 'nv-lifecycle-dot'; }).length; })())"
    ))


def test_a_clean_done_draws_no_label_span_at_all(ctx) -> None:
    """No words is not the whole pin: an empty span would still be a drawn element. A clean done draws no nv-lifecycle-dot at all."""
    _mount(ctx, "done", {"stop_reason": "stop"})
    assert _dot_children(ctx) == 0


def test_an_unclean_done_draws_its_label_in_the_span(ctx) -> None:
    """The span probe is not vacuous: an unclean done draws exactly one nv-lifecycle-dot."""
    _mount(ctx, "done", {"stop_reason": "max_tokens"})
    assert _dot_children(ctx) == 1
