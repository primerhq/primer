"""System > Users: "Disable" asks first, "Enable" does not (ADM-26 of the 2026-10-08 admin review).

"Disable" on a user row fired its PATCH at once, while "Delete" opens a thorough confirmation. A disable is not a small change: the auth middleware treats a disabled
account as unauthenticated from the next request it checks (primer/api/middleware/auth.py), on the cookie path, on the bearer path (an API key resolves to its owner,
who is then refused) and over MCP alike. It checks ONCE when a request or a connection OPENS, so a connection that is already open (a terminal, the tap stream, a session
WebSocket, an MCP stream) is not cut off, and the disable changes one field only (``PATCH /admin/users/{id}`` writes ``disabled``; the session epoch is not bumped), so work
already running for the user (their triggers, running sessions, scheduled fires) is not stopped either. Nothing is deleted and Enable undoes it, which is what the prompt
says. "Enable" only restores access, so it stays one click.

Two pure functions run here in MiniRacer against the real source: ``ADM_toggleConfirm(user)`` (the prompt, or ``null``) and ``ADM_toggleStep(user)`` (what a click does: ask,
or send straight away). The row's handler is JSX, so that it asks BEFORE it sends the PATCH and sends nothing when declined is a source check (this checkout has no render
harness for it), and so is the Delete dialog's wording, which is JSX text. There is no end-to-end journey: the Users page needs an admin session and the ui_e2e lane runs with
authentication disabled.

A double-click on Disable is deliberately not guarded: ``confirmDialog`` is one global slot, so a second call replaces the first dialog and leaves the first call's promise
pending, which duplicates nothing; a guard flag would instead stay set, and Disable would go dead until the row remounts, if that promise were ever orphaned.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "admin_users.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _ctx():
    from py_mini_racer import MiniRacer

    start = SRC.index("function ADM_toggleConfirm(")
    step = SRC.index("function ADM_toggleStep(", start)
    end = SRC.index("\n}\n", step) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(SRC[start:end])
    return ctx


def _prompt(user: dict):
    return json.loads(_ctx().eval(f"JSON.stringify(ADM_toggleConfirm({json.dumps(user)}))"))


def _step(user: dict) -> dict:
    return json.loads(_ctx().eval(f"JSON.stringify(ADM_toggleStep({json.dumps(user)}))"))


def test_disabling_an_enabled_account_asks_first_and_says_what_follows() -> None:
    prompt = _prompt({"id": "u-1", "username": "bob", "disabled": False})

    assert prompt["title"] == "Disable bob?"
    assert prompt["confirmLabel"] == "Disable"
    assert prompt["danger"] is True
    message = prompt["message"]
    assert "starting with the very next one" in message, "it must say when it takes effect"
    assert "API key" in message, "an API key of theirs stops working too"
    assert "Nothing is deleted" in message and "Enable" in message, "it must say it is reversible"


def test_the_prompt_does_not_claim_a_connection_that_is_already_open_is_cut_off() -> None:
    """The middleware checks once when a request or a connection OPENS: an open terminal, tap stream, session WebSocket or MCP stream keeps going. The prompt must
    scope its claim to new requests and connections and say plainly that an open one is not cut off (the lead's review of ADM-26)."""
    message = _prompt({"id": "u-1", "username": "bob", "disabled": False})["message"]

    assert "new requests and connections" in message.lower(), "the refusal applies to NEW requests and connections"
    assert "already open" in message, "it must name the connections it does not reach"
    assert "terminal" in message, "the example an admin cares about: an open terminal"
    assert "not cut off" in message, "it must say they are not cut off"
    assert "Every request this account makes" not in message, "the old claim covered requests on a connection that is already open"
    assert "very next request" not in message, "the wording is 'starting with the very next one'"


def test_the_prompt_says_work_already_running_for_the_user_is_not_stopped() -> None:
    """Disable writes one field: their triggers, running sessions and scheduled fires carry on (the lead's ruling on ADM-26 round 2)."""
    message = _prompt({"id": "u-1", "username": "bob", "disabled": False})["message"]

    assert "work already running" in message
    for what in ("triggers", "running sessions", "scheduled fires"):
        assert what in message, f"{what!r} is work that is not stopped"


def test_the_prompt_says_the_existing_sign_in_works_again_after_enable() -> None:
    """The session is refused, not destroyed, so Enable restores it as it was."""
    message = _prompt({"id": "u-1", "username": "bob", "disabled": False})["message"]

    assert "sign-in" in message, "an existing sign-in works again after Enable"


def test_enabling_a_disabled_account_does_not_ask() -> None:
    """Enable only restores access."""
    assert _prompt({"id": "u-1", "username": "bob", "disabled": True}) is None


@pytest.mark.parametrize("disabled", [None, 0, ""])
def test_an_account_without_a_disabled_flag_is_treated_as_enabled_and_asks(disabled) -> None:
    assert _prompt({"id": "u-1", "username": "bob", "disabled": disabled}) is not None


def test_a_click_on_disable_asks_with_the_prompt() -> None:
    step = _step({"id": "u-1", "username": "bob", "disabled": False})

    assert step["kind"] == "ask"
    assert step["prompt"]["title"] == "Disable bob?"


def test_a_click_on_enable_sends_straight_away() -> None:
    assert _step({"id": "u-1", "username": "bob", "disabled": True}) == {"kind": "send"}


def _handler() -> str:
    body = re.search(r"const toggleDisabled = async \(\) => \{[\s\S]*?\n  \};", SRC)
    assert body, "toggleDisabled is gone"
    return body.group(0)


def test_the_row_asks_before_it_sends_the_patch_and_sends_nothing_when_declined() -> None:
    text = _handler()

    ask = re.search(r"const step = ADM_toggleStep\(user\);\s*if \(step\.kind === \"ask\" && !\(await confirmDialog\(step\.prompt\)\)\) return;", text)
    assert ask, "the handler must take its decision from the tested function, ask with the prompt it returned and stop when the answer is no"
    assert ask.start() < text.index("setBusy(true)"), "a declined prompt must not leave the row busy"
    assert ask.start() < text.index('"PATCH"'), "the prompt comes before the request"


def test_the_row_holds_no_guard_flag_across_the_prompt() -> None:
    """See the module docstring: a flag that a never-settling dialog promise could leave set would make Disable dead until the row remounts."""
    start = SRC.index("function ADM_UserRow(")
    row = SRC[start:SRC.index("const toggleDisabled = async", start)]

    assert "useRef" not in row and "asking" not in row


def test_the_delete_dialog_does_not_say_every_session_is_invalidated() -> None:
    """Delete has the same shape as Disable: the middleware refuses NEW requests and connections from the account, and a connection that is already open (a terminal)
    is not cut off (the lead's ruling on ADM-26 round 2)."""
    start = SRC.index("function ADM_DeleteUserDialog(")
    dialog = SRC[start:SRC.index("// ====", start)]

    assert "invalidated on their next request" not in dialog
    bullet = re.search(r"<li>(New requests[^<]*)</li>", dialog)
    assert bullet, "the first bullet must say what happens to the account's requests"
    text = bullet.group(1)
    assert "starting with the very next one" in text
    assert "already open" in text and "terminal" in text and "not cut off" in text
