"""The terminal panel says "signed out" when the server ended the shell because the account stopped being valid.

The server closes an open terminal WebSocket with code 4401 ``auth_revoked`` when the account behind it is disabled, signed out everywhere, demoted
or its cookie ran out (``primer/api/middleware/revalidate.py``). Every close but 4403 used to read "Connection lost. This is usually transient." with
a Retry button, which is false there: a reconnect meets the same 401 until the user signs in again.

These run the real ``NV_Terminal`` in V8 on the hook runtime in ``tests/ui/_mini_react.py`` with a fake xterm and a fake WebSocket that the test
closes with a chosen code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = ROOT / "ui" / "components" / "console" / "nv-terminal.jsx"

_PRELUDE = r"""
var SOCKS = [];
var __refCalls = 0;
var __con = { wid: "w1", workspaces: [{ id: "w1" }], registry: { get: function () { return null; } } };
function NV_useConsole() { __refCalls = 0; return __con; }
// The host <div> is a DOM node React would hand to the first ref; the hook runtime has no DOM, so the first useRef of each render gets one.
var __useRef = React.useRef;
React.useRef = function (init) {
  var r = __useRef(init);
  __refCalls += 1;
  if (__refCalls === 1 && r.current === null) r.current = {};
  return r;
};
globalThis.document = { documentElement: {} };
globalThis.getComputedStyle = function () { return { getPropertyValue: function () { return ""; } }; };
window.location = { protocol: "http:", host: "console.test" };
window.addEventListener = function () {};
window.removeEventListener = function () {};
window.innerHeight = 900;
window.Terminal = function () {
  this.cols = 80; this.rows = 24;
  this.loadAddon = function () {}; this.open = function () {}; this.write = function () {};
  this.onData = function () {}; this.onResize = function () {}; this.dispose = function () {};
};
window.FitAddon = { FitAddon: function () { this.fit = function () {}; } };
globalThis.WebSocket = function (url) {
  this.url = url; this.readyState = 0; this.closed = false;
  this.send = function () {};
  this.close = function () { this.closed = true; };
  SOCKS.push(this);
};
"""


@pytest.fixture
def mr():
    ctx = mini_react_context(transpile(TERMINAL), _PRELUDE)
    ctx.eval("MR.mount(NV_Terminal, {});")
    try:
        yield ctx
    finally:
        ctx.close()


def _close(ctx, code: int, index: int = -1) -> None:
    ctx.eval(f"SOCKS[{index if index >= 0 else 'SOCKS.length - 1'}].onclose({{ code: {code} }}); MR.rerender();")


def _shown(ctx) -> dict[str, bool]:
    return {
        name: bool(ctx.eval(f'MR.find("nv-terminal-{name}") !== null'))
        for name in ("denied", "signed-out", "conn-lost", "retry", "reconnect")
    }


def test_the_panel_opens_one_socket_and_shows_none_of_the_states(mr) -> None:
    assert mr.eval("SOCKS.length") == 1
    assert not any(_shown(mr).values())


def test_close_code_4401_says_the_user_was_signed_out(mr) -> None:
    _close(mr, 4401)
    shown = _shown(mr)
    assert shown["signed-out"] and not shown["conn-lost"] and not shown["denied"] and not shown["retry"], shown
    text = mr.eval('MR.texts().join(" ")').lower()
    assert "signed out" in text and "sign in again" in text
    assert "transient" not in text, "a revoked session is not a blip"


def test_the_signed_out_notice_is_announced(mr) -> None:
    _close(mr, 4401)
    assert mr.eval('MR.find("nv-terminal-signed-out").props.role') == "alert"


@pytest.mark.parametrize(("code", "expected"), [(4403, "denied"), (1006, "conn-lost"), (1011, "conn-lost"), (1000, "conn-lost")])
def test_the_other_closes_read_as_they_did_before(mr, code: int, expected: str) -> None:
    _close(mr, code)
    shown = _shown(mr)
    assert shown[expected] and not shown["signed-out"], (code, shown)


def test_reconnect_opens_a_new_socket_and_clears_the_notice(mr) -> None:
    _close(mr, 4401)
    mr.eval('MR.click("nv-terminal-reconnect"); MR.rerender();')
    assert mr.eval("SOCKS.length") == 2, "reconnecting opens a second socket"
    assert not _shown(mr)["signed-out"]
    _close(mr, 4401)
    assert _shown(mr)["signed-out"], "and a second revocation shows it again"


def test_a_close_our_own_teardown_causes_does_not_show_the_notice(mr) -> None:
    """Retry closes the old socket on purpose; its close event arrives after the new effect has started and must not flip the new state."""
    _close(mr, 4401)
    mr.eval('MR.click("nv-terminal-reconnect"); MR.rerender();')
    _close(mr, 4401, index=0)               # the first socket's late close event
    assert not _shown(mr)["signed-out"], "a stale socket's close stomped the new connection"
