"""Tests for the API keys page in the kit style."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "ui" / "components" / "shared" / "platform-kit.jsx"
TOKENS = ROOT / "ui" / "components" / "api_tokens.jsx"

PRELUDE = r"""
var window = globalThis;
var DATA = {};
var LOG = { calls: [] };

LOG.opts = {};
LOG.refetches = 0;
window.primerApi = {
  useResource: function (key, fetcher, opts) {
    LOG.opts[key] = opts || {};
    var entry = DATA[key] || { items: [] };
    return {
      data: entry,
      loading: false,
      error: null,
      refetch: function () { LOG.refetches += 1; },
    };
  },
  apiFetch: function (method, path, body) {
    LOG.calls.push([method, path, body]);
    return Promise.resolve({
      id: "at-1",
      name: (body && body.name) || "key",
      prefix: "pk_ab",
      scopes: (body && body.scopes) || [],
      plaintext: "pk_ab_secret"
    });
  },
  readRefusal: function (err, fallback) {
    return { code: (err && err.code) || "error", message: (err && err.message) || fallback || "error" };
  }
};

function Icon(p) {
  return React.createElement("span", { className: "ico", "data-icon": p.name });
}

function Btn(p) {
  var props = Object.assign({}, p);
  props["data-testid"] = p["data-testid"] || p.testid;
  return React.createElement("button", props, p.children);
}

// The shared Modal routes Escape, a backdrop click and its X through onClose: the stub keeps the last one's props so a test can call it.
var LAST_MODAL = null;
function Modal(p) {
  LAST_MODAL = p;
  return React.createElement("div", { className: "modal-stub", "data-title": p.title },
    p.title ? React.createElement("h3", null, p.title) : null,
    p.children,
    p.footer ? React.createElement("div", { className: "modal-footer" }, p.footer) : null
  );
}

function Banner(p) {
  return React.createElement("div", { className: "banner-stub", "data-kind": p.kind },
    p.title ? React.createElement("div", null, p.title) : null,
    p.detail ? React.createElement("div", null, p.detail) : null,
    p.actions || null
  );
}

LOG.confirms = [];
var CONFIRM = true;
window.confirmDialog = function (opts) {
  LOG.confirms.push(opts);
  return Promise.resolve(CONFIRM);
};
var CLIPBOARD = [];
var navigator = { clipboard: { writeText: function (t) { CLIPBOARD.push(t); return Promise.resolve(); } } };
var setTimeout = function () { return 0; };

function ROWS(n) {
  var out = [];
  for (var i = 0; i < n; i++) {
    var owner = i % 2 === 0 ? "ana" : "bo";
    out.push({
      id: "at-" + i,
      name: "key-" + i,
      prefix: "pk_" + i,
      scopes: [],
      _ownerId: "u-" + owner,
      _ownerName: owner
    });
  }
  return out;
}
"""


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(KIT) + "\n" + transpile(TOKENS)


@pytest.fixture
def app(code):
    ctx = mini_react_context(code, PRELUDE)

    class App:
        def js(self, expr: str):
            return ctx.eval(expr)

        def data(self, expr: str):
            return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))

        def mount(self, component: str, props: str = "{}") -> None:
            ctx.eval("MR.mount(" + component + ", " + props + ")")

        def has(self, testid: str) -> bool:
            return bool(ctx.eval("!!MR.find(" + json.dumps(testid) + ")"))

        def click(self, testid: str) -> None:
            ctx.eval("MR.click(" + json.dumps(testid) + ")")

        def texts(self) -> list[str]:
            return json.loads(ctx.eval("JSON.stringify(MR.texts())"))

        def text(self) -> str:
            return "".join(self.texts())

        def find_all(self, prefix: str) -> list[dict]:
            return json.loads(ctx.eval("JSON.stringify(MR.findAll(" + json.dumps(prefix) + "))"))

        def settle(self) -> None:
            ctx.eval("void 0;")
            ctx.eval("MR.rerender();")
            ctx.eval("void 0;")
            ctx.eval("MR.rerender();")

    try:
        yield App()
    finally:
        ctx.close()


def test_admin_mode_has_the_kit_header_with_plus_api_key(app):
    app.js('DATA["api-tokens:admin-list"] = { items: ROWS(3) }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    assert app.has("at-page-admin") and app.has("pk-header") and app.has("create-token-btn")
    assert "API keys" in app.texts() and "+ API key" in app.text() and "3 API keys" in app.text()


def test_the_admin_table_has_an_owner_column_and_marks_your_own_keys(app):
    app.js('DATA["api-tokens:admin-list"] = { items: ROWS(2) }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    assert "Owner" in app.texts() and "ana (you)" in app.text() and "bo" in app.texts()


def test_an_unknown_username_never_marks_an_owner_as_you(app):
    app.js('DATA["api-tokens:admin-list"] = { items: ROWS(2) }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: null }')
    assert "(you)" not in app.text()


def test_personal_mode_is_your_api_keys_without_an_owner_column(app):
    app.js('DATA["api-tokens:list"] = { items: ROWS(1) }')
    app.mount("AT_ApiTokensPage", '{ mode: "personal", username: "ana" }')
    assert app.has("at-page-personal") and "Your API keys" in app.texts() and "Owner" not in app.texts()


def test_twenty_five_keys_show_twenty_on_the_first_page(app):
    app.js('DATA["api-tokens:admin-list"] = { items: ROWS(25) }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    assert len(app.find_all("api-token-row-")) == 20


def test_the_filter_text_covers_name_prefix_owner_scopes_and_status(app):
    text = app.data('AT_filterText({ name: "ci", prefix: "pk_ab", _ownerName: "bo", scopes: ["mcp"] })')
    assert all(part in text for part in ("ci", "pk_ab", "bo", "mcp", "active"))


def test_a_filter_that_matches_nothing_says_so(app):
    app.js('DATA["api-tokens:admin-list"] = { items: ROWS(3) }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    app.js('MR.find("at-filter").props.onChange({ target: { value: "zzz-no-such-key" } }); MR.rerender()')
    assert app.has("at-no-match") and "No API keys match the filter." in app.text()


def test_plus_api_key_opens_the_dialog_that_says_it_acts_as_you(app):
    app.js('DATA["api-tokens:admin-list"] = { items: [] }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    app.click("create-token-btn")
    assert app.has("at-create-form") and "This key acts as you, ana." in app.text()


def test_an_admin_key_is_minted_through_the_callers_own_route(app):
    app.js('DATA["api-tokens:admin-list"] = { items: [] }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    app.click("create-token-btn")
    app.js('MR.find("at-name").props.onChange({ target: { value: "ci-bot" } }); MR.rerender()')
    app.click("create-token-submit")
    app.settle()
    assert app.data("LOG.calls") == [["POST", "/auth/tokens", {"name": "ci-bot", "scopes": [], "expires_at": None}]]


def test_without_a_username_there_is_no_acts_as_line(app):
    """The docs embed (ui/components/docs/embed-registry.jsx 'api-token-create') mounts the page with no props."""
    app.js('DATA["api-tokens:list"] = { items: [] }')
    app.mount("AT_ApiTokensPage", "{}")
    app.click("create-token-btn")
    assert app.has("at-create-form") and not app.has("at-acts-as")


def test_an_empty_list_says_so_and_keeps_one_create_button(app):
    app.js('DATA["api-tokens:admin-list"] = { items: [] }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    assert "No API keys yet" in app.texts() and len(app.find_all("create-token-btn")) == 1


# ---- the one-time key dialog asks before an implicit close (audit G1-12) ------------------------------------------------------------------------------

ONE_TIME = 'var CLOSED = 0; MR.mount(AT_PlaintextOneTimeDialog, { token: { plaintext: "pk_secret", name: "ci", prefix: "pk_ab", scopes: [] }, onClose: function () { CLOSED += 1; } })'


def test_escape_the_backdrop_or_the_x_ask_before_the_only_copy_of_the_key_goes(app):
    """The shared Modal sends Escape, a backdrop click and its X through onClose; each would throw away the only copy of the plaintext."""
    app.js("CONFIRM = false; " + ONE_TIME)
    app.js("LAST_MODAL.onClose()")
    app.settle()
    assert app.js("CLOSED") == 0, "a declined confirm keeps the key on screen"
    [confirm] = app.data("LOG.confirms")
    assert confirm["danger"] is True and "only time" in confirm["message"]
    app.js("CONFIRM = true; LAST_MODAL.onClose()")
    app.settle()
    assert app.js("CLOSED") == 1


def test_a_second_implicit_close_while_the_confirm_is_open_asks_only_once(app):
    app.js("CONFIRM = false; " + ONE_TIME)
    app.js("LAST_MODAL.onClose(); LAST_MODAL.onClose();")
    app.settle()
    assert len(app.data("LOG.confirms")) == 1


def test_the_saved_it_button_closes_without_asking(app):
    app.js(ONE_TIME)
    app.click("close-plaintext-btn")
    app.settle()
    assert app.js("CLOSED") == 1 and app.data("LOG.confirms") == []


def test_once_the_key_is_copied_an_implicit_close_does_not_ask(app):
    app.js(ONE_TIME)
    app.click("copy-token-btn")
    app.settle()
    assert app.data("CLIPBOARD") == ["pk_secret"]
    app.js("LAST_MODAL.onClose()")
    app.settle()
    assert app.js("CLOSED") == 1 and app.data("LOG.confirms") == []


def test_a_blocked_fallback_copy_keeps_the_close_guard_armed(app):
    """Plain-HTTP pages have no navigator.clipboard: when execCommand("copy") answers false the key is not copied, so an implicit close still asks."""
    app.js(
        "navigator = {};"
        "var document = { createElement: function () { return { style: {}, focus: function () {}, select: function () {} }; },"
        " body: { appendChild: function () {}, removeChild: function () {} }, execCommand: function () { return false; } };"
    )
    app.js("CONFIRM = false; " + ONE_TIME)
    app.click("copy-token-btn")
    app.settle()
    assert "Copied!" not in app.text()
    app.js("LAST_MODAL.onClose()")
    app.settle()
    assert app.js("CLOSED") == 0 and len(app.data("LOG.confirms")) == 1


# ---- the admin list: every user, a partial failure said, a slower poll (audit G1-13) --------------------------------------------------------------

FAKE_ADMIN_API = r"""
var API_CALLS = [];
var FAIL_FOR = {};
function fakeApi(method, path, body, opts) {
  API_CALLS.push(path);
  if (path === "/admin/users") return Promise.resolve({ items: [{ id: "u1", username: "ana" }, { id: "u2", username: "bo" }] });
  var m = /^\/admin\/users\/([^/]+)\/tokens$/.exec(path);
  if (m && FAIL_FOR[m[1]]) return Promise.reject(FAIL_FOR[m[1]]);
  if (m) return Promise.resolve({ items: [{ id: "t-" + m[1], name: "key-" + m[1] }] });
  return Promise.reject(new Error("unexpected " + path));
}
var GOT = null, ERR = null;
function read() { AT_readAdminTokens(fakeApi, "SIG").then(function (r) { GOT = r; }, function (e) { ERR = e; }); }
"""


def _drain(app) -> None:
    """Let the pending promise jobs run (nothing is mounted, so there is nothing to re-render)."""
    for _ in range(4):
        app.js("void 0;")


def test_the_admin_list_reads_every_user_and_tags_each_key_with_its_owner(app):
    """GET /admin/users answers every user (the router loops the storage's pages); the old ``?limit=200`` was a cap the server never applied."""
    app.js(FAKE_ADMIN_API + "read();")
    _drain(app)
    assert app.data("API_CALLS") == ["/admin/users", "/admin/users/u1/tokens", "/admin/users/u2/tokens"]
    assert app.data("GOT") == {
        "items": [
            {"id": "t-u1", "name": "key-u1", "_ownerId": "u1", "_ownerName": "ana"},
            {"id": "t-u2", "name": "key-u2", "_ownerId": "u2", "_ownerName": "bo"},
        ],
        "failedUsers": [],
    }


def test_a_user_whose_keys_cannot_be_read_is_named_not_silently_dropped(app):
    app.js(FAKE_ADMIN_API + 'FAIL_FOR.u2 = { status: 500, message: "boom" }; read();')
    _drain(app)
    got = app.data("GOT")
    assert [t["id"] for t in got["items"]] == ["t-u1"]
    assert got["failedUsers"] == ["bo"]


def test_an_aborted_read_is_an_abort_not_a_partial_failure(app):
    app.js(FAKE_ADMIN_API + 'var ab = new Error("aborted"); ab.name = "AbortError"; FAIL_FOR.u2 = ab; read();')
    _drain(app)
    assert app.js("GOT") is None and app.js("ERR.name") == "AbortError"


def test_the_page_says_whose_keys_it_could_not_read(app):
    app.js('DATA["api-tokens:admin-list"] = { items: ROWS(2), failedUsers: ["bo", "cy"] }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    assert app.has("at-partial-failure")
    text = app.text()
    assert "bo, cy" in text and "count" in text
    app.js('MR.find("at-partial-failure-retry").props.onClick()')
    assert app.js("LOG.refetches") == 1


def test_when_every_users_keys_fail_there_is_no_empty_state_under_the_banner(app):
    app.js('DATA["api-tokens:admin-list"] = { items: [], failedUsers: ["ana", "bo"] }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    assert app.has("at-partial-failure")
    assert "No API keys yet" not in app.text()


def test_a_complete_read_draws_no_partial_failure_banner(app):
    app.js('DATA["api-tokens:admin-list"] = { items: ROWS(2), failedUsers: [] }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    assert not app.has("at-partial-failure")


def test_the_admin_list_polls_slower_than_the_personal_one(app):
    """The admin list is 1 + N requests a read (one per user); the personal list is one."""
    app.js('DATA["api-tokens:admin-list"] = { items: [] }')
    app.mount("AT_ApiTokensPage", '{ mode: "admin", username: "ana" }')
    assert app.data('LOG.opts["api-tokens:admin-list"]') == {"pollMs": 60000}
    app.js('DATA["api-tokens:list"] = { items: [] }')
    app.mount("AT_ApiTokensPage", '{ mode: "personal", username: "ana" }')
    assert app.data('LOG.opts["api-tokens:list"]') == {"pollMs": 10000}
