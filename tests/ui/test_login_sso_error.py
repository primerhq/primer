"""The login screen turns an SSO failure into a sentence (ADM-24b).

The server now sends a browser whose SSO sign-in failed back to ``/console/?sso_error=<code>``
(``tests/api/test_sso_browser_failures.py``). ``auth.jsx`` reads the code once, removes it from the address bar and
shows a banner: ``_ssoSignInError(code)`` maps a code to ``{title, detail}`` and ``_takeSsoSignInError(location, history)``
reads and strips the parameter. Both are pure (no JSX, no window.* dependency) and run here in MiniRacer against the
real source.

The code list is pinned against ``primer/api/routers/sso.py``: every code a LOGIN can fail with there must have a sentence,
so adding a ``_reject(...)`` without one fails this file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUTH = (ROOT / "ui" / "components" / "auth.jsx").read_text(encoding="utf-8")
SSO_PY = (ROOT / "primer" / "api" / "routers" / "sso.py").read_text(encoding="utf-8")

# Codes only the authenticated link flow raises: the login screen is never where those land.
LINK_ONLY = {"identity_already_linked", "identity_not_found"}

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _helpers_src() -> str:
    start = AUTH.find("// BEGIN sso-sign-in-error")
    end = AUTH.find("// END sso-sign-in-error")
    if start < 0 or end < 0:
        pytest.fail("auth.jsx has no SSO sign-in error helpers (// BEGIN / END sso-sign-in-error)")
    return AUTH[start:end]


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(_helpers_src())
    return ctx


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


# What a person reads for each failure. Each entry is (code, a phrase the title must carry, a phrase the detail must carry).
SENTENCES = [
    ("provider_unreachable", "unavailable", "did not answer"),
    ("provider_not_found", "no longer available", "administrator"),
    ("invalid_state", "did not complete", "Try again"),
    ("missing_code", "not completed", "cancelled"),
    ("sso_validation_failed", "could not be verified", "administrator"),
    ("account_disabled", "disabled", "administrator"),
    ("sso_jit_disabled", "No account", "administrator"),
    ("sso_race_unresolved", "collided", "Try again"),
]


@pytest.mark.parametrize("code,in_title,in_detail", SENTENCES)
def test_each_code_has_its_own_sentence(code: str, in_title: str, in_detail: str) -> None:
    ctx = _ctx()

    got = _js(ctx, f"_ssoSignInError({json.dumps(code)})")

    assert in_title in got["title"], got
    assert in_detail in got["detail"], got
    assert got["requestId"] is None


def test_an_unknown_code_gets_the_generic_sentence_and_the_code_text_is_never_shown() -> None:
    ctx = _ctx()

    got = _js(ctx, '_ssoSignInError("<script>alert(1)</script>")')

    assert got["title"] == "Single sign-on did not complete"
    assert "script" not in json.dumps(got), "an unknown code must not be echoed into the page"


def test_the_code_is_read_and_removed_from_the_address_bar_keeping_the_rest_of_the_url() -> None:
    ctx = _ctx()
    ctx.eval(
        "var calls = [];"
        'var loc = { pathname: "/console/", search: "?sso_error=sso_jit_disabled&keep=1", hash: "#/w/primer?view=platform:agents" };'
        "var hist = { state: { s: 1 }, replaceState: function (state, title, url) { calls.push([state, url]); } };"
    )

    got = _js(ctx, "_takeSsoSignInError(loc, hist)")

    assert got["title"].startswith("No account")
    assert _js(ctx, "calls") == [[{"s": 1}, "/console/?keep=1#/w/primer?view=platform:agents"]]


def test_the_last_parameter_leaves_a_clean_path() -> None:
    ctx = _ctx()
    ctx.eval(
        "var calls = [];"
        'var loc = { pathname: "/console/", search: "?sso_error=missing_code", hash: "" };'
        "var hist = { state: null, replaceState: function (s, t, url) { calls.push(url); } };"
    )

    _js(ctx, "_takeSsoSignInError(loc, hist)")

    assert _js(ctx, "calls") == ["/console/"]


def test_no_error_parameter_changes_nothing() -> None:
    ctx = _ctx()
    ctx.eval(
        "var calls = [];"
        'var loc = { pathname: "/console/", search: "?keep=1", hash: "#/x" };'
        "var hist = { state: null, replaceState: function () { calls.push(1); } };"
    )

    assert _js(ctx, "_takeSsoSignInError(loc, hist)") is None
    assert _js(ctx, "calls") == []


def test_a_browser_without_the_history_api_still_shows_the_message() -> None:
    ctx = _ctx()
    ctx.eval('var loc = { pathname: "/console/", search: "?sso_error=invalid_state", hash: "" };')

    got = _js(ctx, "_takeSsoSignInError(loc, null)")

    assert got["title"] == "Sign-in did not complete"


def test_every_code_a_login_can_fail_with_in_sso_py_has_a_sentence() -> None:
    raised = set(re.findall(r'_reject\(\s*\d+,\s*"([a-z_]+)"', SSO_PY)) - LINK_ONLY
    assert raised, "the pattern found no _reject codes: sso.py changed shape"
    ctx = _ctx()

    known = set(_js(ctx, "Object.keys(_SSO_SIGN_IN_ERRORS)"))

    assert raised <= known, f"codes with no login-screen sentence: {sorted(raised - known)}"
    assert not (known & LINK_ONLY), "a link-only code does not belong on the login screen"


def test_the_login_screen_starts_from_the_error_and_a_later_failure_replaces_it() -> None:
    assert re.search(r"const _SSO_LOGIN_ERROR = _takeSsoSignInError\(window\.location, window\.history\);", AUTH), (
        "the code must be read once, at load, before anything else can rewrite the URL"
    )
    screen = AUTH[AUTH.index("function LoginScreen("):]
    screen = screen[:screen.index("\n}\n")]
    assert "React.useState(_SSO_LOGIN_ERROR)" in screen, "the login screen's banner state must start from the SSO error"
    assert "setServer(null)" in screen, "submitting the form must still clear the banner"
