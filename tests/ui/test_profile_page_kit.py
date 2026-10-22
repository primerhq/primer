"""Tests for the Profile page in the kit style (Revamp 10, Task 10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
FORM_FIELD = ROOT / "ui" / "components" / "shared" / "form-field.jsx"
KIT = ROOT / "ui" / "components" / "shared" / "platform-kit.jsx"
SYSTEM = ROOT / "ui" / "components" / "console" / "nv-system.jsx"

WRONG_PASSWORD = {
    "status": 401,
    "title": "Authentication Failed",
    "detail": "invalid_credentials",
    "envelope": {
        "type": "/errors/authentication-failed",
        "status": 401,
        "detail": "invalid_credentials",
        "extensions": {"error": "invalid_credentials", "request_id": "req-1"},
    },
}


def _run_pure(name: str, expr: str):
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    try:
        ctx.eval("var window = {};")
        ctx.eval((ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8"))
        sys_src = SYSTEM.read_text(encoding="utf-8")
        start = sys_src.index(f"function {name}(")
        end = sys_src.index("\n}\n", start) + 3
        ctx.eval(sys_src[start:end])
        return json.loads(ctx.eval(f"JSON.stringify({expr})"))
    finally:
        ctx.close()


@pytest.mark.parametrize(
    ("form", "errors"),
    [
        (
            {"current": "", "next": "longenough1", "confirm": "longenough1"},
            {"current": "Enter your current password."},
        ),
        (
            {"current": "old-pass-1", "next": "short", "confirm": "short"},
            {"next": "The new password needs at least 8 characters."},
        ),
        (
            {"current": "old-pass-1", "next": "longenough1", "confirm": "longenough2"},
            {"confirm": "The two new passwords do not match."},
        ),
        (
            {"current": "old-pass-1", "next": "longenough1", "confirm": "longenough1"},
            {},
        ),
    ],
)
def test_the_password_form_names_each_problem_under_its_field(form, errors):
    got = _run_pure("NV_passwordFormErrors", f"NV_passwordFormErrors({json.dumps(form)})")
    assert got == errors


@pytest.mark.parametrize(
    ("status", "text"),
    [
        ({"sign_in_method": "password"}, "Signed in with a password"),
        ({"sign_in_method": "sso", "sign_in_provider": "Okta"}, "Signed in with single sign-on (Okta)"),
        ({"sign_in_method": "sso", "sign_in_provider": None}, "Signed in with single sign-on"),
        ({"sign_in_method": "api_token"}, "Signed in with an API key"),
        ({"sign_in_method": "auth_disabled"}, "Sign-in is off on this server"),
        ({}, ""),
        (None, ""),
    ],
)
def test_the_sign_in_line_says_how_this_session_signed_in(status, text):
    got = _run_pure("NV_signInText", f"NV_signInText({json.dumps(status)})")
    assert got == text


PRELUDE = (ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8") + r"""
var window = globalThis;
var DATA = {
  "auth-status": {
    authenticated: true,
    username: "ana",
    role: "admin",
    sign_in_method: "password",
    sign_in_provider: null
  }
};
var REPLY = { ok: {} };
var LOG = { calls: [], toasts: [], at: null, la: 0 };

window.primerApi = window.primerApi || {};

window.primerApi.useResource = function (key) {
  return {
    data: DATA[key] || null,
    loading: !(key in DATA),
    error: null,
    refetch: function () { return Promise.resolve(); }
  };
};

window.primerApi.apiFetch = function (method, path, body, opts) {
  LOG.calls.push([method, path, body]);
  if (REPLY && REPLY.err) return Promise.reject(REPLY.err);
  if (REPLY && REPLY.ok !== undefined) return Promise.resolve(REPLY.ok);
  return Promise.resolve({});
};

var __con = {
  username: "ana",
  role: "admin",
  toast: function (msg) {
    LOG.toasts.push(msg);
  }
};
window.NV_useConsole = function () { return __con; };

window.AT_ApiTokensPage = function (props) {
  LOG.at = props;
  return React.createElement("div", { "data-testid": "at-page-stub" });
};

window.LA_LinkedAccountsPage = function () {
  LOG.la = (LOG.la || 0) + 1;
  return React.createElement("div", { "data-testid": "la-page-stub" });
};

function Icon(p) {
  return React.createElement("span", { className: "ico", "data-icon": p.name });
}

function Btn(p) {
  var props = Object.assign({}, p);
  props["data-testid"] = p["data-testid"] || p.testid;
  return React.createElement("button", props, p.children);
}

var SH_api = {};
function relativeTime() { return ""; }
function NV_identity(x) { return x; }
"""


@pytest.fixture
def page():
    code = (
        transpile(FORM_FIELD)
        + "\n"
        + transpile(KIT)
        + "\n"
        + transpile(SYSTEM)
    )
    ctx = mini_react_context(code, PRELUDE)

    class PageDriver:
        def js(self, expr: str):
            return ctx.eval(expr)

        def data(self, expr: str):
            return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))

        def mount(self, component: str, props: str = "{}") -> None:
            ctx.eval("MR.mount(" + component + ", " + props + ")")

        def has(self, testid: str) -> bool:
            return bool(ctx.eval("!!MR.find(" + json.dumps(testid) + ")"))

        def click(self, testid: str) -> None:
            ctx.eval("MR.click(" + json.dumps(testid) + ");")

        def fill(self, current: str, nxt: str, confirm: str) -> None:
            ctx.eval(
                f"""
                (function() {{
                    var c = MR.find("nv-pw-current");
                    if (c && c.props && c.props.onChange) c.props.onChange({{ target: {{ value: {json.dumps(current)} }} }});
                    var n = MR.find("nv-pw-next");
                    if (n && n.props && n.props.onChange) n.props.onChange({{ target: {{ value: {json.dumps(nxt)} }} }});
                    var f = MR.find("nv-pw-confirm");
                    if (f && f.props && f.props.onChange) f.props.onChange({{ target: {{ value: {json.dumps(confirm)} }} }});
                    MR.rerender();
                }})()
                """
            )

        def settle(self) -> None:
            ctx.eval("void 0;")
            ctx.eval("MR.rerender();")
            ctx.eval("void 0;")
            ctx.eval("MR.rerender();")

        def attr(self, testid: str, name: str):
            return json.loads(
                ctx.eval(
                    f"JSON.stringify({{v: (MR.find({json.dumps(testid)}) ? (MR.find({json.dumps(testid)}).props[{json.dumps(name)}]) : null)}})"
                )
            ).get("v")

        def texts(self) -> list[str]:
            return json.loads(ctx.eval("JSON.stringify(MR.texts())"))

        def text(self) -> str:
            return "".join(self.texts())

        def text_of(self, testid: str) -> str:
            code = f"""
            (function() {{
                var el = MR.find({json.dumps(testid)});
                if (!el) return "";
                var out = [];
                (function walk(n) {{
                    if (n == null || typeof n === "boolean") return;
                    if (typeof n === "string" || typeof n === "number") {{ out.push(String(n)); return; }}
                    if (Array.isArray(n)) {{ n.forEach(walk); return; }}
                    if (n.__el) {{
                        if (typeof n.type === "function" && n.type !== React.Fragment) {{
                            walk(n.out);
                        }} else {{
                            walk(n.children || (n.props && n.props.children));
                        }}
                    }}
                }})(el);
                return out.join("");
            }})()
            """
            return ctx.eval(code)

    driver = PageDriver()
    yield driver
    ctx.close()


def test_the_title_is_profile_alone(page):  # design D7: the title that joined Profile and the username goes
    page.mount("NV_SysProfile")
    assert page.text_of("nv-sys-profile-title") == "Profile"


def test_the_identity_card_shows_name_role_and_how_you_signed_in(page):
    page.js('DATA["auth-status"].sign_in_method = "sso"; DATA["auth-status"].sign_in_provider = "Okta"')
    page.mount("NV_SysProfile")
    assert page.text_of("nv-sys-identity-name") == "ana" and page.text_of("nv-sys-identity-role") == "admin"
    assert page.text_of("nv-sys-identity-sign-in") == "Signed in with single sign-on (Okta)"


def test_the_password_section_has_current_new_and_confirm(page):
    page.mount("NV_SysProfile")
    assert all(page.has(t) for t in ("nv-sys-password", "nv-pw-current", "nv-pw-next", "nv-pw-confirm", "nv-pw-submit"))
    assert "At least 8 characters" in page.texts()
    assert page.attr("nv-pw-confirm", "aria-label") == "Confirm new password"


def test_a_mismatched_confirmation_is_refused_under_the_field_and_sends_nothing(page):  # Review Focus 5
    page.mount("NV_SysProfile")
    page.fill("old-pass-1", "longenough1", "longenough2")
    page.click("nv-pw-submit")
    page.settle()
    assert "The two new passwords do not match." in page.text() and page.data("LOG.calls") == []


def test_an_empty_current_password_is_named_not_ignored(page):  # Review Focus 5
    page.mount("NV_SysProfile")
    page.fill("", "longenough1", "longenough1")
    page.click("nv-pw-submit")
    page.settle()
    assert "Enter your current password." in page.text() and page.data("LOG.calls") == []


def test_a_valid_change_posts_then_clears_the_fields_and_says_so(page):
    page.mount("NV_SysProfile")
    page.fill("old-pass-1", "longenough1", "longenough1")
    page.click("nv-pw-submit")
    page.settle()
    assert page.data("LOG.calls") == [
        [
            "POST",
            "/auth/change-password",
            {"current_password": "old-pass-1", "new_password": "longenough1"},
        ]
    ]
    assert [page.attr(t, "value") for t in ("nv-pw-current", "nv-pw-next", "nv-pw-confirm")] == ["", "", ""]
    assert page.data("LOG.toasts") == ["Password changed"]


def test_a_refused_change_says_why_above_the_button(page):
    page.js(f"REPLY = {{ err: {json.dumps(WRONG_PASSWORD)} }}")  # the live 401 shape of tests/ui/test_profile_page_messages.py
    page.mount("NV_SysProfile")
    page.fill("wrong-pass-1", "longenough1", "longenough1")
    page.click("nv-pw-submit")
    page.settle()
    assert page.text_of("nv-pw-error").startswith("The current password is not right.")


def test_the_page_mounts_your_api_keys_and_linked_accounts(page):
    page.mount("NV_SysProfile")
    assert page.data("LOG.at") == {"mode": "personal", "username": "ana"} and page.data("LOG.la") >= 1
