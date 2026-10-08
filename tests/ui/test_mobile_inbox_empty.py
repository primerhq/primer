"""The empty phone Inbox says what to do next (console review C-040, second half).

A first-time user finishes setup and lands on the Inbox tab of a phone: one grey line, "Nothing needs you right now.", and nothing else on a
blank screen. The Inbox now explains what lives there and offers "Start a session", which takes the user to the Spaces tab and opens its Create
session sheet (the shell hands the request across: the sheet's state belongs to the Spaces panel).

The real ``NV_MobileInboxPanel`` runs in V8 through ``tests/ui/_mini_react.py``; the whole flow is driven at 390 px by
``tests/ui_e2e/test_mobile_inbox_empty_journey.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    start = SHELL.index("function " + name + "(")
    return SHELL[start:SHELL.index("\n}\n", start) + len("\n}\n")]


def _transpiled(source: str) -> str:
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        return bundler._transform(source, "snippet.jsx")
    finally:
        bundler._ctx.close()


@pytest.fixture
def panel():
    code = _transpiled(_function_source("NV_mobileInboxHeading") + _function_source("NV_MobileInboxPanel"))
    ctx = mini_react_context(code, "var __started = 0; function NV_MobileInboxCard(p) { return React.createElement('div', { 'data-testid': 'card:' + p.item.session_id }); }")
    yield ctx
    ctx.close()


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def test_an_empty_inbox_explains_itself_and_offers_to_start_a_session(panel) -> None:
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [], onResolved: function () {}, onStart: function () { __started += 1; } })")
    text = _texts(panel)
    assert "Nothing needs you right now." in text
    assert "wait here" in text and "Start a session" in text, "it says what the Inbox is for, not only that it is empty"
    assert panel.eval("!!MR.find('nv-mob-ib-empty')") and panel.eval("!!MR.find('nv-mob-ib-start')")


def test_the_start_button_asks_the_shell_once_per_tap(panel) -> None:
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [], onResolved: function () {}, onStart: function () { __started += 1; } })")
    panel.eval("MR.click('nv-mob-ib-start')")
    assert panel.eval("__started") == 1


def test_an_inbox_with_items_shows_them_and_no_empty_state(panel) -> None:
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [{ session_id: 's1' }, { session_id: 's2' }], onResolved: function () {}, onStart: function () {} })")
    assert panel.eval("MR.findAll('card:').length") == 2
    assert not panel.eval("!!MR.find('nv-mob-ib-empty')") and not panel.eval("!!MR.find('nv-mob-ib-start')")


def test_without_a_handler_there_is_no_dead_button(panel) -> None:
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [], onResolved: function () {} })")
    assert panel.eval("!!MR.find('nv-mob-ib-empty')") and not panel.eval("!!MR.find('nv-mob-ib-start')")


def test_the_shell_hands_the_request_to_the_spaces_panel_and_the_panel_consumes_it() -> None:
    """Wiring: the Inbox button switches to Spaces and raises a request; Spaces opens its sheet and lowers it, so it does not reopen later."""
    shell = SHELL[SHELL.index("function NV_MobileShell("):]
    assert "onStart={function () { setStartRequested(true); setActiveTab(\"spaces\"); }}" in shell
    assert "<NV_MobileSpaces startRequested={startRequested} onStartHandled={function () { setStartRequested(false); }} />" in shell
    spaces = _function_source("NV_MobileSpaces")
    effect = spaces[spaces.index("props.startRequested"):][:300]
    assert "setCreateOpen(true)" in effect and "props.onStartHandled()" in effect
