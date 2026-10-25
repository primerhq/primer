"""The rules editor flags a room whose "Start sessions from inbound messages" switch is off (round 2 of #932, item 4).

A binding that starts a session in such a room never fires (the router drops it and logs one warning, once per room and
process), so the operator editing rules here must see it: a one-line hint under the trigger row of a room that does not
start sessions, and a warning in the rule dialog when it is about to save a session-starting rule for such a room. The
room state is the stored channel's config.chats.enabled (a missing block answers false, like the backend gate).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "ui" / "components" / "channel_rules.jsx"

PRELUDE = r"""
var window = globalThis;
var DATA = {};
window.primerApi = {
  useResource: function (key, fetcher, opts) {
    return { data: DATA[key], loading: false, error: null, refetch: function () {} };
  },
  apiFetch: function (method, path) { return Promise.resolve({ items: [] }); },
  useMutation: function (fn, opts) { return { mutate: function () {}, loading: false }; },
  useViewport: function () { return { isMobile: false }; }
};
function Icon(p) { return React.createElement("span", { className: "ico" }); }
function Btn(p) { var pr = Object.assign({}, p); pr["data-testid"] = p.testid || null; return React.createElement("button", pr, p.children); }
function Modal(p) { return React.createElement("div", { className: "modal-stub" }, p.children); }
function Banner(p) { return React.createElement("div", { className: "banner-stub", "data-kind": p.kind }, p.title, p.detail); }
function Card(p) { return React.createElement("div", p.children); }
function CardList(p) { return React.createElement("div", p.children); }
function Fab(p) { return null; }
function PR_fetchAllPages(path) { return Promise.resolve({ items: [] }); }
function PR_PagedNote(p) { return null; }
DATA["cr-providers"] = { items: [{ id: "cp-1", provider: "slack" }] };
DATA["cr-channels"] = { items: [
  { id: "ch-off", config: { chats: { enabled: false } } },
  { id: "ch-on", config: { chats: { enabled: true } } },
  { id: "ch-bare", config: null }
]};
DATA["cr-triggers"] = { items: [
  { id: "tr-off", config: { kind: "channel", provider_id: "cp-1", channel_id: "ch-off" } },
  { id: "tr-on", config: { kind: "channel", provider_id: "cp-1", channel_id: "ch-on" } },
  { id: "tr-bare", config: { kind: "channel", provider_id: "cp-1", channel_id: "ch-bare" } }
]};
DATA["cr-subs-tr-off"] = { items: [{ id: "sb-1", event_matcher: { event_type: "message.posted" }, config: { kind: "agent_fresh_session" }, reply_target: null }] };
DATA["cr-subs-tr-on"] = { items: [] };
DATA["cr-subs-tr-bare"] = { items: [{ id: "sb-2", event_matcher: { event_type: "message.posted" }, config: { kind: "session_append" }, reply_target: null }] };
"""


def _app(code: str):
    ctx = mini_react_context(code, PRELUDE)

    class App:
        def js(self, expr: str):
            return ctx.eval(expr)

        def texts(self) -> list[str]:
            return json.loads(ctx.eval("JSON.stringify(MR.texts())"))

        def find(self, testid: str):
            return ctx.eval("MR.find(" + json.dumps(testid) + ")") is not None

        def click(self, testid: str) -> None:
            ctx.eval("MR.click(" + json.dumps(testid) + ")")
            ctx.eval("MR.rerender();")

        def settle(self) -> None:
            ctx.eval("MR.rerender();")

    return App(), ctx


def test_the_row_hint_shows_only_on_rooms_that_do_not_start_sessions() -> None:
    code = transpile(RULES)
    app, ctx = _app(code)
    try:
        app.js("MR.mount(window.ChannelRulesPage, {});")
        app.settle()
        # The room with chats.enabled false and the room with no config block (the gate reads both as "does not start
        # sessions") each carry the one-line hint.
        assert app.find("channel-rule-room-disabled-hint-tr-off")
        assert app.find("channel-rule-room-disabled-hint-tr-bare")
        # The room with the switch on gets no hint.
        assert not app.find("channel-rule-room-disabled-hint-tr-on")
        hint = " ".join(app.texts())
        assert "Start sessions from inbound messages" in hint
        assert "session_append and parked_session bindings still do" in hint
    finally:
        ctx.close()


def test_the_rule_dialog_warns_about_a_disabled_room() -> None:
    code = transpile(RULES)
    app, ctx = _app(code)
    try:
        app.js("MR.mount(window.ChannelRulesPage, {});")
        app.settle()
        assert not any("Room does not start sessions" in t for t in app.texts()), "the warning shows before a room is chosen"
        app.click("cr-new-rule")
        assert not any("Room does not start sessions" in t for t in app.texts()), "a blank room (all rooms) gets no warning"
        app.js(
            'var el = MR.findProp("placeholder", "(blank = all rooms)");'
            "if (!el) throw new Error('no room input');"
            'el.props.onChange({ target: { value: "ch-off" } });'
        )
        app.settle()
        assert any("Room does not start sessions" in t for t in app.texts()), "the dialog must name the switch it is being saved against"
        app.js(
            'var el = MR.findProp("placeholder", "(blank = all rooms)");'
            'el.props.onChange({ target: { value: "ch-on" } });'
        )
        app.settle()
        assert not any("Room does not start sessions" in t for t in app.texts()), "an enabled room clears the warning"
    finally:
        ctx.close()
