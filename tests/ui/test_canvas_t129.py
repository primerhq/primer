"""T129 (GU-2, GU-3, GU-21, GU-22, GU-24 on the browser side; GU-22 and GU-23 on the server are in tests/canvas)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from py_mini_racer import MiniRacer

from tests.ui._canvas_dom import canvas_dom_context, js

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def ctx():
    context = canvas_dom_context()
    yield context
    context.close()


def els(**elements) -> str:
    out = {"root": {"type": "Column", "props": {}, "children": list(elements)}}
    out.update(elements)
    return json.dumps(out)


def events(ctx):
    return [m for m in js(ctx, "FD.sent()") if m["type"] == "event"]


def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start : src.index("\n}\n", start) + 3]


# ---- GU-2: a busy Button is aria-disabled (it keeps focus) and the console clears it -------------------------------------------------------


def test_a_busy_button_is_aria_disabled_not_natively_disabled_and_refuses_a_second_click(ctx):
    ctx.eval("FD.connect();")
    ctx.eval(f"FD.state({els(go={'type': 'Button', 'props': {'label': 'Next'}})});")
    ctx.eval("FD.byId('go').click(); FD.byId('go').click();")
    assert len(events(ctx)) == 1
    assert js(ctx, "[FD.byId('go').disabled, FD.byId('go').attrs['aria-disabled'], FD.byId('go').attrs['aria-busy']]") == [False, "true", "true"]
    ctx.eval("FD.post({type: 'busy', element: 'go', on: false});")
    assert js(ctx, "[FD.byId('go').disabled, FD.byId('go').attrs['aria-disabled'] || null, FD.byId('go').attrs['aria-busy'] || null]") == [False, None, None]
    ctx.eval("FD.byId('go').click();")
    assert len(events(ctx)) == 2


def test_a_button_the_agent_disabled_stays_natively_disabled(ctx):
    ctx.eval("FD.connect();")
    ctx.eval(f"FD.state({els(off={'type': 'Button', 'props': {'label': 'Off', 'disabled': True}})});")
    assert js(ctx, "FD.byId('off').disabled") is True


def test_the_console_clears_a_busy_element_when_rev_or_event_seq_moves() -> None:
    src = (ROOT / "ui" / "components" / "console" / "nv-canvas.jsx").read_text(encoding="utf-8")
    made = MiniRacer()
    made.eval(_fn(src, "NV_canvasBusyCleared"))
    made.eval("var pending = {a: {rev: 3, seq: 1}, b: {rev: 3, seq: 1}, c: {rev: 4, seq: 1}};")
    assert json.loads(made.eval("JSON.stringify(NV_canvasBusyCleared(pending, {rev: 4, event_seq: 1}))")) == ["a", "b"]
    assert json.loads(made.eval("JSON.stringify(NV_canvasBusyCleared(pending, {rev: 3, event_seq: 2}))")) == ["a", "b", "c"]
    assert json.loads(made.eval("JSON.stringify(NV_canvasBusyCleared(pending, {rev: 3, event_seq: 1}))")) == ["c"]
    assert json.loads(made.eval("JSON.stringify(NV_canvasBusyCleared(pending, null))")) == []
    assert 'type: "busy"' in src and "on: false" in src


# ---- GU-3: a canvas the server accepts is drawn, a refused state is reported ----------------------------------------------------------------


def test_a_state_up_to_the_settings_ceiling_is_accepted_and_a_larger_one_is_reported(ctx):
    body = "<p>" + "x" * 250_000 + "</p>"
    elements = {"root": {"type": "Column", "props": {}, "children": [f"h{i}" for i in range(10)]}}
    for i in range(10):
        elements[f"h{i}"] = {"type": "Html", "props": {"html": body}}
    ctx.eval("FD.connect();")
    ctx.eval(f"FD.state({json.dumps(elements)});")
    assert js(ctx, "FD.byId('h9') !== null"), "2.5 MB of Html under the 4 MiB ceiling is drawn"
    too_big = {"root": {"type": "Column", "props": {}, "children": ["h"]}, "h": {"type": "Text", "props": {"text": "y" * (7 * 1024 * 1024)}}}
    ctx.eval(f"FD.state({json.dumps(too_big)}, {{rev: 2}});")
    problems = [m["message"] for m in js(ctx, "FD.sent()") if m["type"] == "error"]
    assert any("could not be drawn" in p for p in problems), problems


# ---- GU-21: deleting an earlier sibling does not re-insert an Html frame --------------------------------------------------------------------


def test_removing_an_earlier_sibling_does_not_take_an_html_holder_out_and_put_it_back(ctx):
    ctx.eval("FD.connect();")
    both = {"root": {"type": "Column", "props": {}, "children": ["note", "form"]},
            "note": {"type": "Text", "props": {"text": "Loading"}},
            "form": {"type": "Html", "props": {"html": "<input>"}}}
    ctx.eval(f"FD.state({json.dumps(both)}, {{rev: 1}});")
    ctx.eval("var holder = FD.byId('form');")
    before = js(ctx, "holder.inserted")
    only = {"root": {"type": "Column", "props": {}, "children": ["form"]}, "form": {"type": "Html", "props": {"html": "<input>"}}}
    ctx.eval(f"FD.state({json.dumps(only)}, {{rev: 2}});")
    assert js(ctx, "holder.inserted") == before
    assert js(ctx, "FD.byId('note')") is None


def test_reordering_still_moves_only_what_is_out_of_place(ctx):
    ctx.eval("FD.connect();")
    ctx.eval(f"FD.state({els(a={'type': 'Text', 'props': {'text': 'A'}}, b={'type': 'Text', 'props': {'text': 'B'}})});")
    ctx.eval(f"FD.state({els(b={'type': 'Text', 'props': {'text': 'B'}}, a={'type': 'Text', 'props': {'text': 'A'}})}, {{rev: 2}});")
    assert js(ctx, "FD.root.textContent") == "BA"


# ---- GU-22: a disabled container disables what is inside it ---------------------------------------------------------------------------------


def test_a_button_inside_a_disabled_card_is_disabled_and_sends_nothing(ctx):
    ctx.eval("FD.connect();")
    tree = {"root": {"type": "Column", "props": {}, "children": ["card", "free"]},
            "card": {"type": "Card", "props": {"disabled": True}, "children": ["inner"]},
            "inner": {"type": "Button", "props": {"label": "Submit"}},
            "free": {"type": "Button", "props": {"label": "Free"}}}
    ctx.eval(f"FD.state({json.dumps(tree)});")
    assert js(ctx, "[FD.byId('inner').disabled, FD.byId('free').disabled]") == [True, False]
    ctx.eval("FD.byId('inner').click(); FD.byId('free').click();")
    assert [e["element"] for e in events(ctx)] == ["free"]
    # lifting the container's flag enables the control again
    tree["card"]["props"] = {}
    ctx.eval(f"FD.state({json.dumps(tree)}, {{rev: 2}});")
    assert js(ctx, "FD.byId('inner').disabled") is False


# ---- GU-24: a throwing Html element toasts once ----------------------------------------------------------------------------------------------


def test_the_console_toasts_a_repeated_canvas_error_once() -> None:
    src = (ROOT / "ui" / "components" / "console" / "nv-canvas.jsx").read_text(encoding="utf-8")
    made = MiniRacer()
    made.eval(_fn(src, "NV_canvasErrorIsNew"))
    made.eval("var seen = {};")
    assert made.eval("NV_canvasErrorIsNew(seen, 'TypeError: x is undefined')") is True
    assert made.eval("NV_canvasErrorIsNew(seen, 'TypeError: x is undefined')") is False
    assert made.eval("NV_canvasErrorIsNew(seen, 'Another error')") is True
    for i in range(20):
        made.eval(f"NV_canvasErrorIsNew(seen, 'e{i}')")
    assert made.eval("NV_canvasErrorIsNew(seen, 'one more distinct')") is False, "a bounded number of distinct toasts per frame"
    assert "NV_canvasErrorIsNew(" in src.split('if (m.type === "error")', 1)[1][:200]
