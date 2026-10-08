"""A session that does not exist is a state of its own in the session document (console review 2026-10-08, C-020).

The pure decision (a 404 on the session row, with or without data kept from before) and the card are run in V8; the wiring in the real
document, and the polls that stop, are checked in the browser by ``tests/ui_e2e/test_missing_session_journey.py``.
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


@pytest.fixture
def gone_ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(_function("NV_isSessionGone"))
    try:
        yield ctx
    finally:
        ctx.close()


def _gone(ctx, error) -> bool:
    return ctx.eval("NV_isSessionGone(" + json.dumps(error) + ")")


def test_a_404_on_the_session_row_means_it_is_gone(gone_ctx) -> None:
    assert _gone(gone_ctx, {"status": 404, "title": "Not Found"}) is True


@pytest.mark.parametrize("error", [None, {}, {"status": 500}, {"status": 401}, {"status": 403}, {"status": 408}, {"status": 429}, {"status": 503},
                                   {"name": "TypeError", "message": "Failed to fetch"}, {"status": "404"}])
def test_nothing_but_a_404_means_it_is_gone(gone_ctx, error) -> None:
    """A blip, a refusal or a server error is not the session ceasing to exist: the document keeps what it has and keeps trying."""
    assert _gone(gone_ctx, error) is False, error


def _card_context():
    from primer.api._jsx_bundle import JSXBundler

    ui = ROOT / "ui"
    bundler = JSXBundler(ui_dir=ui, babel_source=(ui / "vendor" / "babel.min.js").read_text())
    try:
        code = bundler._transform(_function("NV_SessionGone"), "snippet.jsx")
    finally:
        bundler._ctx.close()
    return mini_react_context(code, "var CLOSED = 0;")


@pytest.fixture
def card():
    ctx = _card_context()
    ctx.eval("MR.mount(NV_SessionGone, { sid: 'sess-x', onClose: function () { CLOSED += 1; } });")
    try:
        yield ctx
    finally:
        ctx.close()


def test_the_card_says_the_session_no_longer_exists_and_names_it(card) -> None:
    text = card.eval("MR.texts().join(' ')")
    assert "no longer exists" in text and "sess-x" in text
    assert card.eval('MR.find("nv-session-gone") !== null')


def test_the_card_offers_to_close_the_tab_and_nothing_else_to_do(card) -> None:
    assert card.eval('MR.find("nv-session-gone-close") !== null')
    card.eval('MR.click("nv-session-gone-close");')
    assert card.eval("CLOSED") == 1
    assert not card.eval('MR.find("nv-composer") !== null'), "a missing session has no composer"


def test_the_card_is_a_polite_status_and_not_an_alert(card) -> None:
    """Arriving by a link is not an emergency, and a restored tab must not shout."""
    assert card.eval('MR.find("nv-session-gone").props.role') == "status"
