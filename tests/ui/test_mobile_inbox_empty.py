"""The empty phone Inbox says what to do next (console review C-040, second half).

A first-time user finishes setup and lands on the Inbox tab of a phone: one grey line, "Nothing needs you right now.", and nothing else on a
blank screen. The Inbox now explains what lives there and offers "Start a session", which takes the user to the Spaces tab and opens its Create
session sheet (the shell hands the request across: the sheet's state belongs to the Spaces panel).

The empty state is only true once the Inbox HAS loaded. While the first fetch is in flight, and when it fails, the panel says so (a loading line, or the
error with Try again) instead of "Nothing needs you right now." and a call to action: that line is a claim about the user's queue, and a failed fetch
cannot make it (review of PR 549). The heading's "Nothing waiting on you" waits for data for the same reason.

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
ATTENTION = (ROOT / "ui" / "foundation" / "shell-attention.js").read_text(encoding="utf-8")  # SH_pendingId, which keys the cards by their gate


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
    ctx = mini_react_context(code, "var window = globalThis;\n" + ATTENTION + "\nvar __started = 0; var __retried = 0; function NV_errText(e) { return e ? (e.detail || e.message || 'Request failed') : null; } function NV_MobileInboxCard(p) { return React.createElement('div', { 'data-testid': 'card:' + p.item.session_id }); }")
    yield ctx
    ctx.close()


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def test_an_empty_inbox_explains_itself_and_offers_to_start_a_session(panel) -> None:
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [], onResolved: function () {}, loaded: true, onStart: function () { __started += 1; } })")
    text = _texts(panel)
    assert "Nothing needs you right now." in text
    assert "wait here" in text and "Start a session" in text, "it says what the Inbox is for, not only that it is empty"
    assert panel.eval("!!MR.find('nv-mob-ib-empty')") and panel.eval("!!MR.find('nv-mob-ib-start')")


def test_the_start_button_asks_the_shell_once_per_tap(panel) -> None:
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [], onResolved: function () {}, loaded: true, onStart: function () { __started += 1; } })")
    panel.eval("MR.click('nv-mob-ib-start')")
    assert panel.eval("__started") == 1


def test_an_inbox_with_items_shows_them_and_no_empty_state(panel) -> None:
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [{ session_id: 's1' }, { session_id: 's2' }], onResolved: function () {}, loaded: true, onStart: function () {} })")
    assert panel.eval("MR.findAll('card:').length") == 2
    assert not panel.eval("!!MR.find('nv-mob-ib-empty')") and not panel.eval("!!MR.find('nv-mob-ib-start')")


def test_without_a_handler_there_is_no_dead_button(panel) -> None:
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [], onResolved: function () {}, loaded: true })")
    assert panel.eval("!!MR.find('nv-mob-ib-empty')") and not panel.eval("!!MR.find('nv-mob-ib-start')")


def test_the_shell_hands_the_request_to_the_spaces_panel_and_the_panel_consumes_it() -> None:
    """Wiring: the Inbox button switches to Spaces and raises a request; Spaces opens its sheet and lowers it, so it does not reopen later."""
    shell = SHELL[SHELL.index("function NV_MobileShell("):]
    assert "onStart={function () { setStartRequested(true); setActiveTab(\"spaces\"); }}" in shell
    assert "<NV_MobileSpaces startRequested={startRequested} onStartHandled={function () { setStartRequested(false); }} />" in shell
    spaces = _function_source("NV_MobileSpaces")
    effect = spaces[spaces.index("props.startRequested"):][:300]
    assert "setCreateOpen(true)" in effect and "props.onStartHandled()" in effect


# --- the empty state is a claim about the queue: it needs the queue to have loaded ----------------------------------------------------------

_PENDING = "MR.mount(NV_MobileInboxPanel, { items: [], loaded: false, error: %s, onResolved: function () {}, onStart: function () { __started += 1; }, onRetry: function () { __retried += 1; } })"


def _mount_unloaded(panel, error: str = "null") -> None:
    panel.eval(_PENDING.replace("%s", error))


def test_while_the_first_fetch_is_in_flight_the_panel_says_loading_and_makes_no_claim(panel) -> None:
    _mount_unloaded(panel)
    text = _texts(panel)
    assert panel.eval("!!MR.find('nv-mob-ib-loading')")
    assert not panel.eval("!!MR.find('nv-mob-ib-empty')") and not panel.eval("!!MR.find('nv-mob-ib-start')")
    assert "Nothing needs you" not in text and "Nothing waiting on you" not in text, "an unloaded queue is not an empty one"


def test_a_failed_first_fetch_shows_the_error_and_a_retry_not_the_empty_state(panel) -> None:
    _mount_unloaded(panel, "{ detail: 'The server could not be reached.' }")
    assert panel.eval("!!MR.find('nv-mob-ib-error')")
    text = _texts(panel)
    assert "The server could not be reached." in text and "Try again" in text
    assert not panel.eval("!!MR.find('nv-mob-ib-empty')") and not panel.eval("!!MR.find('nv-mob-ib-start')")
    assert "Nothing needs you" not in text and "Nothing waiting on you" not in text
    assert panel.eval("MR.find('nv-mob-ib-error').props.role") == "alert"


def test_the_retry_button_asks_for_the_inbox_again(panel) -> None:
    _mount_unloaded(panel, "{ message: 'boom' }")
    panel.eval("MR.click('nv-mob-ib-retry')")
    assert panel.eval("__retried") == 1


def test_a_failed_poll_after_a_good_load_keeps_what_was_loaded(panel) -> None:
    """Stale-while-error: the queue was loaded once, so its empty state stays (a later poll failing does not turn it into an error screen)."""
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [], loaded: true, error: { message: 'boom' }, onResolved: function () {}, onStart: function () {} })")
    assert panel.eval("!!MR.find('nv-mob-ib-empty')") and not panel.eval("!!MR.find('nv-mob-ib-error')")


def test_items_are_shown_even_when_an_error_is_present(panel) -> None:
    panel.eval("MR.mount(NV_MobileInboxPanel, { items: [{ session_id: 's1' }], loaded: true, error: { message: 'boom' }, onResolved: function () {} })")
    assert panel.eval("MR.findAll('card:').length") == 1 and not panel.eval("!!MR.find('nv-mob-ib-error')")


def test_the_heading_does_not_claim_an_empty_queue_before_it_has_loaded(panel) -> None:
    ctx = panel
    assert ctx.eval("NV_mobileInboxHeading(0, false).count") == ""
    assert ctx.eval("NV_mobileInboxHeading(0, true).count") == "Nothing waiting on you"
    assert ctx.eval("NV_mobileInboxHeading(0).count") == "Nothing waiting on you", "the one-argument form keeps meaning a loaded queue"
    assert ctx.eval("NV_mobileInboxHeading(3, false).count") == "3 waiting on you"


def test_the_shell_gives_the_panel_what_the_resource_knows() -> None:
    shell = SHELL[SHELL.index("function NV_MobileShell("):]
    assert "loaded={inboxRes.data != null}" in shell
    assert "error={inboxRes.error}" in shell
    assert "onRetry={inboxRes.refetch}" in shell
