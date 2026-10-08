"""System > Users: "Disable" asks first, "Enable" does not (ADM-26 of the 2026-10-08 admin review).

"Disable" on a user row fired its PATCH at once, while "Delete" opens a thorough confirmation. A disable is not a small change: the auth middleware treats a disabled
account as unauthenticated from the next request it checks (primer/api/middleware/auth.py), on the cookie path, on the bearer path (an API key resolves to its owner,
who is then refused) and over MCP alike. It checks ONCE when a request or a connection OPENS, so a connection that is already open (a terminal, the tap stream, a session
WebSocket, an MCP stream) is not cut off. Nothing is deleted and Enable undoes it, which is what the prompt says. "Enable" only restores access, so it stays one click.

Two pure functions run here in MiniRacer against the real source: ``ADM_toggleConfirm(user)`` (the prompt, or ``null``) and ``ADM_toggleStep(user, asking)`` (what a click does:
ignore it while a prompt is already open, ask, or send straight away). The row's handler is JSX, so that it asks BEFORE it sends the PATCH, sends nothing when declined and
holds the ``asking`` flag across the prompt is a source check (this checkout has no render harness for it). There is no end-to-end journey: the Users page needs an admin
session and the ui_e2e lane runs with authentication disabled.
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


def _step(user: dict, asking: bool) -> dict:
    return json.loads(_ctx().eval(f"JSON.stringify(ADM_toggleStep({json.dumps(user)}, {str(asking).lower()}))"))


def test_disabling_an_enabled_account_asks_first_and_says_what_follows() -> None:
    prompt = _prompt({"id": "u-1", "username": "bob", "disabled": False})

    assert prompt["title"] == "Disable bob?"
    assert prompt["confirmLabel"] == "Disable"
    assert prompt["danger"] is True
    message = prompt["message"]
    assert "very next request" in message, "it must say when it takes effect"
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
    step = _step({"id": "u-1", "username": "bob", "disabled": False}, asking=False)

    assert step["kind"] == "ask"
    assert step["prompt"]["title"] == "Disable bob?"


def test_a_click_on_enable_sends_straight_away() -> None:
    assert _step({"id": "u-1", "username": "bob", "disabled": True}, asking=False) == {"kind": "send"}


@pytest.mark.parametrize("disabled", [False, True])
def test_a_second_click_while_a_prompt_is_open_is_ignored(disabled: bool) -> None:
    """A double-click on Disable must not call confirmDialog a second time (the row is not busy until the answer is yes). confirmDialog is one global slot, so the second
    call would replace the first dialog and leave the first call's promise pending for good; ignoring it keeps the first dialog, and its handler, as the one that is answered."""
    assert _step({"id": "u-1", "username": "bob", "disabled": disabled}, asking=True) == {"kind": "ignore"}


def _handler() -> str:
    body = re.search(r"const toggleDisabled = async \(\) => \{[\s\S]*?\n  \};", SRC)
    assert body, "toggleDisabled is gone"
    return body.group(0)


def test_the_row_asks_before_it_sends_the_patch_and_sends_nothing_when_declined() -> None:
    text = _handler()

    step = re.search(r"const step = ADM_toggleStep\(user, asking\.current\);\s*if \(step\.kind === \"ignore\"\) return;", text)
    assert step, "the handler must take its decision from the tested function and do nothing while a prompt is open"
    ask = re.search(r"if \(step\.kind === \"ask\"\) \{[\s\S]*?confirmed = await confirmDialog\(step\.prompt\);[\s\S]*?\}\s*if \(!confirmed\) return;", text)
    assert ask, "the handler must ask with the prompt the function returned and stop when the answer is no"
    assert step.start() < ask.start() < text.index("setBusy(true)"), "a declined prompt must not leave the row busy"
    assert ask.start() < text.index('"PATCH"'), "the prompt comes before the request"


def test_the_asking_flag_is_held_across_the_prompt_and_released_whatever_the_answer() -> None:
    """A ref (not state): the second click must see it before React re-renders. Released in a finally, so an answer of yes or no, or a dialog that throws, cannot leave
    Disable dead. Not covered: a promise that never settles (another confirmDialog call replacing this one in the global slot) would keep the flag set."""
    assert re.search(r"const asking = React\.useRef\(false\);", SRC), "the row keeps an `asking` ref"
    text = _handler()
    ask = re.search(
        r"asking\.current = true;\s*let confirmed = false;\s*try \{\s*confirmed = await confirmDialog\(step\.prompt\);\s*\} finally \{\s*asking\.current = false;\s*\}",
        text,
    )
    assert ask, "set before the await, cleared in a finally after it"
