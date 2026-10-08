"""A workspace that does not exist is a state of its own in the Studio (console review 2026-10-08, C-019).

The pure decision (a 404 on the workspace row) and the card are run in V8; the wiring in the real Studio, and the requests that no longer
fire, are checked in the browser by ``tests/ui_e2e/test_unknown_workspace_journey.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
STUDIO = (ROOT / "ui" / "components" / "console" / "nv-studio.jsx").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = STUDIO.index("function " + name + "(")
    return STUDIO[start:STUDIO.index("\n}\n", start) + len("\n}\n")]


@pytest.fixture
def gone_ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(_function("NV_isWorkspaceGone"))
    try:
        yield ctx
    finally:
        ctx.close()


def test_a_404_on_the_workspace_row_means_it_is_gone(gone_ctx) -> None:
    assert gone_ctx.eval('NV_isWorkspaceGone({status: 404, title: "Not Found"})') is True


@pytest.mark.parametrize("error", [None, {}, {"status": 500}, {"status": 401}, {"status": 403}, {"status": 408}, {"status": 429}, {"status": 503},
                                   {"name": "TypeError", "message": "Failed to fetch"}, {"status": "404"}])
def test_nothing_but_a_404_means_it_is_gone(gone_ctx, error) -> None:
    """A blip, a refusal or a server error is not the workspace ceasing to exist: the Studio keeps drawing what it has."""
    assert gone_ctx.eval("NV_isWorkspaceGone(" + json.dumps(error) + ")") is False, error


def _card(workspaces: list[dict]):
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        code = bundler._transform(_function("NV_WorkspaceGone"), "snippet.jsx")
    finally:
        bundler._ctx.close()
    ctx = mini_react_context(code, "var OPENED = [];")
    ctx.eval("MR.mount(NV_WorkspaceGone, { wid: 'nope', workspaces: " + json.dumps(workspaces) + ", onOpen: function (id) { OPENED.push(id); } });")
    return ctx


@pytest.fixture
def card():
    made = []

    def build(workspaces: list[dict]):
        ctx = _card(workspaces)
        made.append(ctx)
        return ctx

    try:
        yield build
    finally:
        for ctx in made:
            ctx.close()


def test_the_card_names_the_workspace_that_was_not_found(card) -> None:
    ctx = card([{"id": "w1", "name": "Alpha"}])
    text = ctx.eval("MR.texts().join(' ')")
    assert "'nope' was not found" in text
    assert ctx.eval('MR.find("nv-ws-gone").props.role') == "status"


def test_the_card_offers_a_way_back_to_each_real_workspace(card) -> None:
    ctx = card([{"id": "w1", "name": "Alpha"}, {"id": "w2", "name": None}])
    assert ctx.eval('MR.find("nv-ws-gone-open:w1") !== null') and ctx.eval('MR.find("nv-ws-gone-open:w2") !== null')
    assert "Alpha" in ctx.eval("MR.texts().join(' ')") and "w2" in ctx.eval("MR.texts().join(' ')")
    ctx.eval('MR.click("nv-ws-gone-open:w2");')
    assert json.loads(ctx.eval("JSON.stringify(OPENED)")) == ["w2"]


def test_the_card_with_no_workspace_to_go_to_offers_no_buttons(card) -> None:
    ctx = card([])
    assert ctx.eval('MR.findAll("nv-ws-gone-open:").length') == 0
    assert "was not found" in ctx.eval("MR.texts().join(' ')")


def test_the_card_lists_a_bounded_number_of_workspaces(card) -> None:
    ctx = card([{"id": f"w{i:02d}", "name": f"W{i}"} for i in range(30)])
    assert ctx.eval('MR.findAll("nv-ws-gone-open:").length') == 8
