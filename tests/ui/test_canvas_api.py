"""The canvas REST calls (pivot P5, Task 19): SH_api's canvas methods name the
S6 routes (primer/api/routers/canvas.py, #908) with the exact method and path -
GET/DELETE the session's canvas, POST /canvas/ops, POST /canvas/events, and the
admin GET/PUT /canvas_settings - and the canvas read's cache key names the
session.

Loaded the way test_gate_token_console.py loads sh-api.jsx: a MiniRacer context
with a recording apiFetch, so the calls asserted are the calls, not substrings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import transpile

ROOT = Path(__file__).resolve().parents[2]
SH_API = ROOT / "ui" / "components" / "shell" / "sh-api.jsx"


def _js(ctx, expr):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def api():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis; var CALLS = [];"
             "window.primerApi = { apiFetch: function (method, path, body) { CALLS.push([method, path, body]); return Promise.resolve({}); } };")
    ctx.eval(transpile(SH_API))
    try:
        yield ctx
    finally:
        ctx.close()


def test_the_canvas_calls_name_their_urls(api) -> None:
    """Each method calls apiFetch with the exact method and path of Task 15
    (S6, #908): the canvas read and clear are the session's canvas route, the
    operator batch is /ops, a person's click or choice is /events (a 202), and
    the settings are the admin's GET/PUT /canvas_settings."""
    api.eval(
        "SH_api.canvas('w1', 's1');"
        "SH_api.canvasOps('w1', 's1', { ops: [] });"
        "SH_api.canvasClear('w1', 's1');"
        "SH_api.canvasEvent('w1', 's1', { client_event_id: 'e1' });"
        "SH_api.canvasSettings();"
        "SH_api.putCanvasSettings({ strict_mode: false });"
    )
    assert _js(api, "CALLS") == [
        ["GET", "/workspaces/w1/sessions/s1/canvas", None],
        ["POST", "/workspaces/w1/sessions/s1/canvas/ops", {"ops": []}],
        ["DELETE", "/workspaces/w1/sessions/s1/canvas", None],
        ["POST", "/workspaces/w1/sessions/s1/canvas/events", {"client_event_id": "e1"}],
        ["GET", "/canvas_settings", None],
        ["PUT", "/canvas_settings", {"strict_mode": False}],
    ]


def test_the_ids_are_encoded_in_the_canvas_paths(api) -> None:
    api.eval("SH_api.canvas('w/1', 's:1'); SH_api.canvasClear('w/1', 's:1');")
    assert [c[1] for c in _js(api, "CALLS")] == [
        "/workspaces/w%2F1/sessions/s%3A1/canvas",
        "/workspaces/w%2F1/sessions/s%3A1/canvas",
    ]


def test_the_canvas_read_key_names_the_session(api) -> None:
    assert _js(api, "SH_api.keys.canvas('s1')") == "nv-canvas:s1"
