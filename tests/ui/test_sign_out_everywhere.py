"""The profile menu offers "Sign out everywhere" (SEC-05).

Static pins: the menu row exists next to "Log out", confirms first (it
ends the user's other sessions too) and posts to the revoke-all endpoint.
GU-12: a sign-out reloads to the sign-in screen only when the server ended the
session (or said it already was); any other answer is toasted and the page stays,
so a user who pressed "Sign out everywhere" is not told nothing while no session ended.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHROME = (ROOT / "ui" / "components" / "console" / "nv-chrome.jsx").read_text(encoding="utf-8")


def _row() -> str:
    m = re.search(r'data-testid="nv-logout-all"[\s\S]{0,900}', CHROME)
    assert m, "the profile menu has no sign-out-everywhere row"
    return m.group(0)


def test_the_row_sits_after_log_out():
    assert CHROME.index('data-testid="nv-logout"') < CHROME.index('data-testid="nv-logout-all"')


def test_the_row_confirms_then_signs_out_through_the_shared_helper():
    row = _row()
    assert "Sign out everywhere" in row
    assert "window.confirmDialog(" in row  # the themed dialog, never the browser's confirm()
    assert "window.confirm(" not in row
    assert 'NV_signOut(con, "/auth/logout-all")' in row
    assert "fetch(" not in row


# ---- GU-12: the helper both sign-out buttons share, executed ----------------------------------------------------------------------------------

MOBILE = (ROOT / "ui" / "components" / "console" / "nv-mobile-shell.jsx").read_text(encoding="utf-8")


def _helper() -> str:
    start = CHROME.index("async function NV_signOut(")
    return CHROME[start:CHROME.index("\n}\n", start) + 3]


def _ctx(outcome: str):
    """The real helper in V8; ``outcome`` is JS for what ``apiFetch`` does."""
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(
        "var window = globalThis; var CALLS = [], TOASTS = [], RELOADS = 0;"
        "window.location = { reload: function () { RELOADS += 1; } };"
        "window.primerApi = {"
        "  apiFetch: function (m, p) { CALLS.push(m + ' ' + p); return " + outcome + "; },"
        "  readRefusal: function (err, fb) { return { message: (err && err.detail) || fb }; } };"
        "var con = { toast: function (m) { TOASTS.push(m); } };"
    )
    ctx.eval(_helper().replace("async function NV_signOut", "var NV_signOut = async function"))
    return ctx


def _sign_out(ctx, path: str) -> dict:
    import json

    ctx.eval(f"var DONE = false; NV_signOut(con, {json.dumps(path)}).then(function () {{ DONE = true; }});")
    for _ in range(4):
        ctx.eval("void 0;")
    return {"calls": json.loads(ctx.eval("JSON.stringify(CALLS)")), "toasts": json.loads(ctx.eval("JSON.stringify(TOASTS)")),
            "reloads": ctx.eval("RELOADS")}


def test_a_signed_out_answer_reloads_to_the_sign_in_screen():
    ctx = _ctx("Promise.resolve({})")
    try:
        assert _sign_out(ctx, "/auth/logout-all") == {"calls": ["POST /auth/logout-all"], "toasts": [], "reloads": 1}
    finally:
        ctx.close()


def test_an_already_signed_out_401_reloads_too():
    ctx = _ctx("Promise.reject({ status: 401, detail: 'not signed in' })")
    try:
        assert _sign_out(ctx, "/auth/logout")["reloads"] == 1
    finally:
        ctx.close()


def test_a_5xx_toasts_the_refusal_and_does_not_reload():
    ctx = _ctx("Promise.reject({ status: 503, detail: 'could not end your sessions' })")
    try:
        out = _sign_out(ctx, "/auth/logout-all")
        assert out["reloads"] == 0 and out["toasts"] == ["Couldn't sign out: could not end your sessions"], out
    finally:
        ctx.close()


def test_both_shells_sign_out_through_the_helper_and_no_raw_fetch_is_left():
    assert 'NV_signOut(con, "/auth/logout")' in CHROME and 'NV_signOut(con, "/auth/logout")' in MOBILE
    for src in (CHROME, MOBILE):
        assert 'fetch("/v1/auth/logout' not in src
