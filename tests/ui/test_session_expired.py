"""An expired or revoked session is noticed once, globally (review 8.1).

Before: no 401 branch anywhere. The cookie session ended (expiry, a disabled user, sign-out everywhere), every poll answered 401, and the console
kept showing the last session list and Inbox as current with no way back to the sign-in screen but a manual reload.

Now ``apiFetch`` (ui/foundation/api.js) answers a 401 from any route but /auth/* by reading ``/auth/status`` once; when that says the session is
over it sets ``primerApi.sessionExpired`` (``useResource`` stops polling and refetching) and fires ``primer:session-expired``; ``AuthGate``
(ui/components/auth.jsx) drops the console for the sign-in screen with a "signed out" notice, which unmounts every poll and stream.

The real api.js and the real AuthGate run in V8 through tests/ui/_mini_react.py, with a scripted ``fetch``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8")
AUTH = ROOT / "ui" / "components" / "auth.jsx"

_PRELUDE = r"""
window.React = window.React || {};
var FETCHED = []; var STATUS = { has_user: true, authenticated: false }; var ROUTES = {}; var EXPIRED_EVENTS = 0;
globalThis.fetch = function (url) {
  FETCHED.push(url);
  var path = String(url);
  if (path === "/v1/auth/status") return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(STATUS); } });
  var status = ROUTES[path] || 200;
  return Promise.resolve({ ok: status < 400, status: status, statusText: "x", json: function () { return Promise.resolve(status < 400 ? { items: [] } : { status: status, title: "Unauthorized" }); } });
};
window.addEventListener("primer:session-expired", function () { EXPIRED_EVENTS += 1; });
window.location = { search: "", pathname: "/console/", hash: "", href: "http://x/console/", reload: function () {} };
window.history = { replaceState: function () {} };
var document = { title: "", documentElement: { getAttribute: function () { return "light"; } } };
"""


@pytest.fixture
def api_ctx():
    c = mini_react_context(API, _PRELUDE)
    try:
        yield c
    finally:
        c.close()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


def _call(ctx, path: str) -> str:
    """apiFetch(path) as a settled outcome: the resolved value or the error's status."""
    ctx.eval(
        "var OUT = null; window.primerApi.apiFetch('GET', " + json.dumps(path) + ").then(function () { OUT = 'ok'; },"
        " function (e) { OUT = 'error:' + e.status; });"
    )
    ctx.eval("void 0;")
    return js(ctx, "OUT")


def test_a_401_with_an_ended_session_is_noticed_once_for_many_requests(api_ctx) -> None:
    api_ctx.eval("ROUTES['/v1/sessions'] = 401; ROUTES['/v1/workspaces'] = 401;")
    for _ in range(3):
        assert _call(api_ctx, "/sessions") == "error:401"
    assert _call(api_ctx, "/workspaces") == "error:401"
    assert js(api_ctx, "FETCHED.filter(function (u) { return u === '/v1/auth/status'; }).length") == 1, "one status read, however many 401s"
    assert js(api_ctx, "EXPIRED_EVENTS") == 1
    assert js(api_ctx, "window.primerApi.sessionExpired") is True
    # Once noticed, a later 401 does not read the status or fire the event again.
    _call(api_ctx, "/sessions")
    assert js(api_ctx, "EXPIRED_EVENTS") == 1


def test_a_401_the_status_does_not_confirm_is_only_that_requests_error(api_ctx) -> None:
    # A route relaying an upstream 401 while the console's own session is fine.
    api_ctx.eval("STATUS = { has_user: true, authenticated: true }; ROUTES['/v1/toolsets/x/probe'] = 401;")
    assert _call(api_ctx, "/toolsets/x/probe") == "error:401"
    assert js(api_ctx, "EXPIRED_EVENTS") == 0 and js(api_ctx, "!!window.primerApi.sessionExpired") is False
    # And the console still checks again on the next 401 (the probe is not latched by a negative answer).
    _call(api_ctx, "/toolsets/x/probe")
    assert js(api_ctx, "FETCHED.filter(function (u) { return u === '/v1/auth/status'; }).length") == 2


def test_a_wrong_password_and_the_status_read_itself_never_signal_expiry(api_ctx) -> None:
    api_ctx.eval("ROUTES['/v1/auth/login'] = 401; ROUTES['/v1/auth/status'] = 401;")
    assert _call(api_ctx, "/auth/login") == "error:401"
    assert js(api_ctx, "FETCHED") == ["/v1/auth/login"], "a failed sign-in is not a probe"
    assert js(api_ctx, "EXPIRED_EVENTS") == 0


def test_other_failures_do_not_probe(api_ctx) -> None:
    api_ctx.eval("ROUTES['/v1/agents'] = 403; ROUTES['/v1/graphs'] = 500;")
    assert _call(api_ctx, "/agents") == "error:403" and _call(api_ctx, "/graphs") == "error:500"
    assert js(api_ctx, "FETCHED") == ["/v1/agents", "/v1/graphs"]


@pytest.fixture
def gate():
    c = mini_react_context(transpile(AUTH), _PRELUDE_GATE)
    c.eval("function Host() { return React.createElement(AuthGate, null, 'APP'); }")
    c.eval("MR.mount(Host, {});")
    c.eval("MR.rerender();")
    c.eval("MR.rerender();")
    try:
        yield c
    finally:
        c.close()


_PRELUDE_GATE = r"""
window.location = { search: "", pathname: "/console/", hash: "", href: "http://x/console/" };
window.history = { replaceState: function () {} };
var document = { title: "", documentElement: { getAttribute: function () { return "light"; } } };
var __timers = [];
globalThis.setTimeout = function (fn, ms) { __timers.push({ fn: fn, ms: ms }); return __timers.length; };
globalThis.clearTimeout = function () {};
window.primerApi = { apiFetch: function () { return Promise.resolve({ has_user: true, authenticated: true, setup_complete: true }); } };
"""


def _texts(ctx) -> str:
    return " ".join(json.loads(ctx.eval("JSON.stringify(MR.texts())")))


def test_the_gate_drops_the_console_for_the_sign_in_screen_and_says_why(gate) -> None:
    assert "APP" in _texts(gate), "signed in: the console"
    gate.eval("window.dispatchEvent(new CustomEvent('primer:session-expired')); MR.rerender();")
    text = _texts(gate)
    assert "APP" not in text, "the console, and with it every poll and stream, is gone"
    assert "You have been signed out" in text and "Sign in to your console" in text
    assert "no longer current" in text


def test_the_gate_stops_listening_when_it_leaves_the_tree(gate) -> None:
    gate.eval(
        "var __removed = 0; var __remove = window.removeEventListener;"
        " window.removeEventListener = function (t, f) { __removed += 1; return __remove(t, f); };"
        " function Gone() { return null; } MR.mount(Gone, {}); MR.rerender();"
    )
    assert js(gate, "__removed") >= 1, "the gate's listener is removed"
    gate.eval("window.dispatchEvent(new CustomEvent('primer:session-expired'));")  # nothing listens: no throw
