"""The sign-in form says what a throttled attempt is (architecture review A-07).

``POST /v1/auth/login`` answers 429 ``too_many_attempts`` after repeated failures (``tests/auth/test_login_throttle.py``). Without a
branch of its own the form would fall through to the generic "Request failed" banner; with one it says the attempts were
throttled and shows the server's message, which carries the number of seconds to wait.
"""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"
SRC = UI / "components" / "auth.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_a_429_has_its_own_banner_that_is_not_the_invalid_password_one() -> None:
    src = _src()
    start = src.index("function _extractServerError")
    body = src[start:src.index("function RegisterScreen")]
    throttled = body.index("status === 429")
    assert body.index("status === 401") < throttled, "the 429 branch must not be swallowed by the 401 one"
    assert "Too many sign-in attempts" in body[throttled:]
    assert "err.detail" in body[throttled:], "the server message carries the seconds to wait"


def test_auth_jsx_transpiles() -> None:
    from primer.api._jsx_bundle import JSXBundler

    b = JSXBundler(ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text())
    code = b._transform(_src(), "components/auth.jsx")
    assert code and "Too many sign-in attempts" in code
