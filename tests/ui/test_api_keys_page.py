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

window.primerApi = {
  useResource: function (key, fetcher, opts) {
    var entry = DATA[key] || { items: [] };
    return {
      data: entry,
      loading: false,
      error: null,
      refetch: function () {},
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

function Modal(p) {
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
