"""The registration form tells the truth and says what is wrong in its own words (console review C-002 and C-006, 2026-10-08).

* C-002: the card said "SSO and additional users land in a later release", the opposite of the console, which ships System > Users and
  System > SSO. It now says the first account is the administrator and where the others are added.
* C-006: a password under 8 characters read "value must have at least 8 characters" (the framework's generic noun), and a username with a
  space went to the server and came back as a "Validation Error" banner with a request id although the placeholder already states the rule.
  The form now checks the username shape itself, with the server's rule (``primer/api/routers/auth.py`` ``_USERNAME_RE``, applied after
  ``strip().lower()``), and names the field in every message.

``AUTH_registerErrors`` and ``AUTH_passwordChangeErrors`` are pure and run in V8; the real ``RegisterScreen`` runs on the hook runtime in
``tests/ui/_mini_react.py`` to show a bad username never reaches the network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "ui" / "components" / "auth.jsx"
SERVER = ROOT / "primer" / "api" / "routers" / "auth.py"

_PRELUDE = r"""
window.location = { search: "", pathname: "/console/", hash: "" };
window.history = { state: null, replaceState: function () {} };
var CALLS = [];
window.primerApi = { apiFetch: function (method, path) { CALLS.push(method + " " + path); return Promise.resolve({}); } };
var ELS = [];
var __ce = React.createElement;
React.createElement = function (type, props) {
  var el = __ce.apply(null, arguments);
  if (typeof type === "string") {
    var text = Array.prototype.slice.call(arguments, 2).filter(function (c) { return typeof c === "string"; }).join("");
    ELS.push({ type: type, props: props || {}, text: text });
  }
  return el;
};
function __draw() { ELS.length = 0; MR.rerender(); }
function __first(pred) { return ELS.filter(pred)[0]; }
"""


@pytest.fixture(scope="module")
def code() -> str:
    return transpile(AUTH)


@pytest.fixture
def ctx(code):
    c = mini_react_context(code, _PRELUDE)
    try:
        yield c
    finally:
        c.close()


def _errors(ctx, fn: str, **fields: str) -> dict[str, str]:
    return json.loads(ctx.eval(f"JSON.stringify({fn}({json.dumps(fields)}))"))


def test_the_first_account_card_does_not_say_users_and_sso_do_not_exist_yet() -> None:
    src = AUTH.read_text(encoding="utf-8")
    assert "later release" not in src, "System > Users and System > SSO ship"
    assert "This first account is the administrator" in src
    assert "System" in src[src.index("This first account is the administrator"):][:200]


@pytest.mark.parametrize("name", ["reviewer", "Reviewer", " reviewer ", "ok.name_1-x", "a", "x" * 64])
def test_a_username_the_server_accepts_passes(ctx, name: str) -> None:
    got = _errors(ctx, "AUTH_registerErrors", username=name, password="long-enough", confirm="long-enough")
    assert got == {}


@pytest.mark.parametrize("name", ["Reviewer Name", "rév", "a/b", "x" * 65, "name!", "tab\tname"])
def test_a_username_the_server_would_reject_is_named_before_the_request(ctx, name: str) -> None:
    got = _errors(ctx, "AUTH_registerErrors", username=name, password="long-enough", confirm="long-enough")
    assert list(got) == ["username"]
    assert "lowercase letters, digits" in got["username"] and "64" in got["username"]
    assert "Validation" not in got["username"]


def test_an_empty_username_is_required_not_malformed(ctx) -> None:
    got = _errors(ctx, "AUTH_registerErrors", username="  ", password="long-enough", confirm="long-enough")
    assert got == {"username": "username is required"}


def test_the_client_rule_is_the_servers_rule() -> None:
    """The console copies the server's regex; if the server's changes, this fails instead of the form drifting."""
    server = re.search(r'_USERNAME_RE = re\.compile\(r"(.+?)"\)', SERVER.read_text(encoding="utf-8")).group(1)
    assert server in AUTH.read_text(encoding="utf-8"), "ui/components/auth.jsx must test the same pattern as _USERNAME_RE"


def test_a_short_password_says_password_not_value(ctx) -> None:
    got = _errors(ctx, "AUTH_registerErrors", username="reviewer", password="short", confirm="short")
    assert got == {"password": "password must have at least 8 characters"}
    assert _errors(ctx, "AUTH_registerErrors", username="reviewer", password="12345678", confirm="12345678") == {}


def test_a_mismatched_confirmation_is_named_and_errors_are_reported_together(ctx) -> None:
    got = _errors(ctx, "AUTH_registerErrors", username="Bad Name", password="short", confirm="other")
    assert set(got) == {"username", "password", "confirm"}
    assert got["confirm"] == "passwords don't match"


def test_the_forced_password_change_says_which_password(ctx) -> None:
    got = _errors(ctx, "AUTH_passwordChangeErrors", current="", next="short", confirm="other")
    assert got == {
        "current": "current password is required",
        "next": "new password must have at least 8 characters",
        "confirm": "passwords don't match",
    }
    assert _errors(ctx, "AUTH_passwordChangeErrors", current="x", next="long-enough", confirm="long-enough") == {}


def test_no_message_in_the_auth_forms_uses_the_generic_word_value() -> None:
    assert "value must" not in AUTH.read_text(encoding="utf-8")


def _type(ctx, index: int, value: str) -> None:
    """Type into the index-th text or password input of the form (username, password, confirmation), through its own onChange."""
    ctx.eval(
        f"ELS.filter(function (e) {{ return e.type === 'input'; }})[{index}]"
        f".props.onChange({{ target: {{ value: {json.dumps(value)} }} }}); __draw();"
    )


def test_a_bad_username_is_shown_by_the_form_and_never_sent(ctx) -> None:
    ctx.eval("MR.mount(RegisterScreen, { onDone: function () {} }); __draw();")
    _type(ctx, 0, "Reviewer Name")
    _type(ctx, 1, "long-enough")
    _type(ctx, 2, "long-enough")
    ctx.eval("__first(function (e) { return e.type === 'form'; }).props.onSubmit({ preventDefault: function () {} }); __draw();")
    shown = json.loads(ctx.eval("JSON.stringify(MR.texts())"))
    assert any("lowercase letters, digits" in t for t in shown), shown
    calls = json.loads(ctx.eval("JSON.stringify(CALLS)"))
    assert "POST /auth/register" not in calls, f"a username the server would refuse must not be sent: {calls}"
