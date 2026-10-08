"""A failed ``GET /v1/auth/status`` is an error with a retry, never "no account exists" (console review follow-up, 2026-10-08).

``AuthGate``'s catch used to set ``{has_user: false, authenticated: false, setup_complete: false}``, so a timeout, a 5xx or a network blip
on an install that has users rendered "Create the operator account" (and, once a status did arrive, a wizard or a login on a guess). A
failed read says nothing about whether an account exists. The gate now shows what failed, retries on its own, and offers a button.

``AUTH_failureView`` is evaluated in V8; the gate itself is driven in a real browser by ``tests/ui_e2e/test_auth_gate_status_failure_journey.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

AUTH = (Path(__file__).resolve().parents[2] / "ui" / "components" / "auth.jsx").read_text(encoding="utf-8")


def _gate() -> str:
    return AUTH[AUTH.index("function AuthGate("):AUTH.index("function _AuthBrand(")]


@pytest.fixture
def failure_view():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    start = AUTH.index("function AUTH_failureView")
    ctx.eval(AUTH[start:AUTH.index("\n}\n", start) + len("\n}\n")])
    try:
        yield lambda err: json.loads(ctx.eval("JSON.stringify(AUTH_failureView(" + json.dumps(err) + "))"))
    finally:
        ctx.close()


def test_a_network_failure_says_the_server_cannot_be_reached(failure_view) -> None:
    got = failure_view({"name": "TypeError", "message": "Failed to fetch"})
    assert got["title"] == "Cannot reach the server"
    assert "retrying" in got["detail"].lower()
    assert "Failed to fetch" not in got["title"] + got["detail"], "the browser's own wording is not user language"


def test_the_shape_apifetch_gives_a_dead_connection_is_a_network_failure(failure_view) -> None:
    """foundation/api.js turns a failed fetch into an ApiError with status 0, which is not "an unexpected answer"."""
    got = failure_view({"name": "ApiError", "status": 0, "type": "/errors/network-error"})
    assert got["title"] == "Cannot reach the server"


def test_a_server_error_says_the_server_answered_with_an_error_and_keeps_the_request_id(failure_view) -> None:
    got = failure_view({"name": "ApiError", "status": 503, "requestId": "req-9"})
    assert got["title"] == "The server is not ready"
    assert "503" in got["detail"] and "retrying" in got["detail"].lower()
    assert got["requestId"] == "req-9"


def test_any_other_answer_is_named_without_claiming_anything_about_accounts(failure_view) -> None:
    got = failure_view({"name": "ApiError", "status": 404})
    assert "404" in got["detail"]
    for text in (got["title"], got["detail"]):
        assert "account" not in text.lower() and "register" not in text.lower()


def test_the_gate_no_longer_guesses_an_install_state_when_the_read_fails() -> None:
    """The catch branch must not fabricate ANY status: it used to say no user exists and setup is incomplete."""
    gate = _gate()
    catch = gate[gate.index("} catch"):]
    catch = catch[:catch.index("})();")]
    assert "setStatus" not in catch and "has_user" not in catch and "setup_complete" not in catch
    assert "AUTH_failureView(err)" in catch and "setFailure(view)" in catch


def test_the_gate_shows_the_failure_and_retries_on_its_own_and_on_a_click() -> None:
    gate = _gate()
    assert "if (failure)" in gate and "AuthUnreachableScreen" in gate
    assert "setTimeout(" in gate and "AUTH_RETRY_MS" in AUTH
    assert 'data-testid="auth-retry"' in AUTH and 'data-testid="auth-unreachable"' in AUTH


def test_a_failure_screen_is_chosen_before_any_register_login_or_wizard_branch() -> None:
    gate = _gate()
    assert gate.index("if (failure)") < gate.index("status.authenticated") < gate.index("RegisterScreen")
