"""System > Profile says what is wrong in words, and its empty states describe what is there (ADM-25 and ADM-27 of the 2026-10-08 admin review).

ADM-25: a wrong current password in Change password showed the machine code ``invalid_credentials`` as the only message. ``POST /v1/auth/change-password`` answers it with ``401``
and a problem envelope whose ``detail`` is the code and whose ``extensions.error`` repeats it (read from a live response); the page printed ``err.detail`` as it came. The same
401 also answers an account with NO password (single sign-on only) and a lost race with another change, so the sentence must not claim more than "the current password is not
right"; and an EXPIRED session answers 401 as well, with another detail, which must not be told as a wrong password: the mapping is on the code, not on the status.

ADM-27: the Linked accounts empty state said "Link a single sign-on provider below", but with no provider configured there is nothing below; the API tokens empty state offered a
second "Create token" button next to the header's.

``NV_passwordChangeMessage`` (``nv-system.jsx``) and ``LA_emptyHint`` (``linked_accounts.jsx``) are pure and run in MiniRacer on the real source; the components are JSX, so how they
use them is a source check (this checkout has no render harness for them), and ``tests/ui_e2e/test_profile_page_journey.py`` drives the real page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SYSTEM = (ROOT / "ui" / "components" / "console" / "nv-system.jsx").read_text(encoding="utf-8")
LINKED = (ROOT / "ui" / "components" / "linked_accounts.jsx").read_text(encoding="utf-8")
TOKENS = (ROOT / "ui" / "components" / "api_tokens.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _run(source: str, name: str, expression: str):
    from py_mini_racer import MiniRacer

    start = source.index(f"function {name}(")
    end = source.index("\n}\n", start) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(source[start:end])
    return json.loads(ctx.eval(f"JSON.stringify({expression})"))


def _message(err) -> str:
    return _run(SYSTEM, "NV_passwordChangeMessage", f"NV_passwordChangeMessage({json.dumps(err)})")


# The shape of the real response (POST /v1/auth/change-password with a wrong current password, read from a live server).
WRONG_PASSWORD = {
    "status": 401, "title": "Authentication Failed", "detail": "invalid_credentials",
    "envelope": {"type": "/errors/authentication-failed", "status": 401, "detail": "invalid_credentials", "extensions": {"error": "invalid_credentials", "request_id": "req-1"}},
}


# ---- ADM-25 ------------------------------------------------------------------------------------------------------------------------------------------------------


def test_a_wrong_current_password_is_told_in_words_not_as_a_code() -> None:
    message = _message(WRONG_PASSWORD)

    assert message.startswith("The current password is not right.")
    assert "invalid_credentials" not in message


def test_the_sentence_does_not_claim_the_one_cause_the_same_401_has_three() -> None:
    """Also an account with no password (single sign-on only) and a lost race with another change."""
    assert "single sign-on" in _message(WRONG_PASSWORD)


def test_the_code_is_read_from_the_envelope_extensions_too() -> None:
    err = {"status": 401, "detail": "something else", "envelope": {"extensions": {"error": "invalid_credentials"}}}

    assert _message(err).startswith("The current password is not right.")


def test_an_expired_session_is_not_told_as_a_wrong_password() -> None:
    """A 401 for another reason keeps the server's own words: the mapping is on the code, not on the status."""
    err = {"status": 401, "detail": "Authentication required.", "envelope": {"status": 401, "detail": "Authentication required.", "extensions": {}}}

    message = _message(err)
    assert "Authentication required." in message and "current password" not in message


@pytest.mark.parametrize("detail", ["Too many attempts. Try again in 30 seconds.", "New password needs at least 8 characters."])
def test_a_server_sentence_is_kept_as_it_is(detail: str) -> None:
    assert _message({"status": 429, "detail": detail}) == detail


def test_the_clients_own_short_password_message_passes_through() -> None:
    """The page builds ``{message: ...}`` itself for a new password under 8 characters."""
    assert _message({"message": "New password needs at least 8 characters."}) == "New password needs at least 8 characters."


@pytest.mark.parametrize("code", ["some_new_code", "password_reuse_blocked"])
def test_an_unknown_machine_code_is_never_shown_bare(code: str) -> None:
    message = _message({"status": 400, "detail": code})

    assert message != code
    assert message.startswith("Could not change the password") and code in message


def test_a_failure_with_no_detail_says_so() -> None:
    assert _message(None) == "Could not change the password."
    assert _message({}) == "Could not change the password."
    assert _message({"message": "Failed to fetch"}) == "Failed to fetch"


def test_the_page_prints_the_mapped_message_not_the_raw_detail() -> None:
    body = SYSTEM[SYSTEM.index("function NV_SysProfile("):SYSTEM.index("// nav -> body.")]

    assert re.search(r'<div className="nv-form-error">\{NV_passwordChangeMessage\(err\)\}</div>', body)
    assert "err.detail || err.message" not in body


# ---- ADM-27: linked accounts -----------------------------------------------------------------------------------------------------------------------------


def _hint(providers: list, loaded: bool) -> str:
    return _run(LINKED, "LA_emptyHint", f"LA_emptyHint({json.dumps(providers)}, {json.dumps(loaded)})")


def test_with_a_provider_configured_the_hint_points_at_the_list_below() -> None:
    assert _hint([{"id": "okta", "name": "Okta"}], True) == "Link a single sign-on provider below to sign in without a password."


def test_with_no_provider_configured_the_hint_says_where_one_is_added() -> None:
    hint = _hint([], True)

    assert "below" not in hint, "there is nothing below"
    assert hint == "No single sign-on provider is configured on this server. An administrator adds them under System > SSO."


def test_before_the_providers_are_known_the_hint_says_nothing_rather_than_the_wrong_thing() -> None:
    """The list loads separately and the first render has none: it must not flash 'none configured'."""
    assert _hint([], False) == ""


def test_the_empty_state_uses_the_hint_and_only_a_successful_providers_read_marks_them_known() -> None:
    page = LINKED[LINKED.index("function LA_LinkedAccountsPage("):]

    assert "LA_emptyHint(providers, providersLoaded)" in page
    assert 'data-testid="linked-accounts-empty"' in page and "No linked accounts yet" in page
    read = re.search(r"try \{\s*const r = await apiFetch\(\"GET\", \"/auth/sso/providers\"[\s\S]*?\} catch", page)
    assert read, "the providers read is gone"
    assert "setProvidersLoaded(true)" in read.group(0), "a successful read, even an empty one, makes the providers known"
    after_catch = page[read.end():page.index("}, [apiFetch]);", read.end())]
    assert "setProvidersLoaded(true)" not in after_catch, "a FAILED read must not turn into a claim that none are configured"


# ---- ADM-27: API tokens ----------------------------------------------------------------------------------------------------------------------------------------


def test_the_api_tokens_empty_state_does_not_repeat_the_headers_create_button() -> None:
    page = TOKENS[TOKENS.index("function AT_ApiTokensPage("):]
    empty = page[page.index('<div className="head">No API tokens yet</div>'):]
    empty = empty[:empty.index("{items.length > 0 && (")]

    assert "Create token" not in empty and "setCreateOpen" not in empty, "the header already has the button"
    assert 'data-testid="create-token-btn"' in page[:page.index("No API tokens yet")], "and the header keeps it"
