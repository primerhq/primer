"""System > Users: "Disable" asks first, "Enable" does not (ADM-26 of the 2026-10-08 admin review).

"Disable" on a user row fired its PATCH at once, while "Delete" opens a thorough confirmation. A disable is not a small change: the auth middleware treats a disabled
account as unauthenticated from its VERY NEXT request ("deactivation takes effect on the very next request", primer/api/middleware/auth.py), on the cookie path, on
the bearer path (an API key resolves to its owner, who is then refused) and over MCP alike. Nothing is deleted and Enable undoes it, which is what the prompt says.
"Enable" only restores access, so it stays one click.

The decision is a pure function, ``ADM_toggleConfirm(user)``, that runs here in MiniRacer against the real source; the row's handler is JSX, so that it asks BEFORE it
sends the PATCH and sends nothing when declined is a source check (this checkout has no render harness for it). There is no end-to-end journey: the Users page needs an
admin session and the ui_e2e lane runs with authentication disabled.
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


def _prompt(user: dict):
    from py_mini_racer import MiniRacer

    start = SRC.index("function ADM_toggleConfirm(")
    end = SRC.index("\n}\n", start) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(SRC[start:end])
    return json.loads(ctx.eval(f"JSON.stringify(ADM_toggleConfirm({json.dumps(user)}))"))


def test_disabling_an_enabled_account_asks_first_and_says_what_follows() -> None:
    prompt = _prompt({"id": "u-1", "username": "bob", "disabled": False})

    assert prompt["title"] == "Disable bob?"
    assert prompt["confirmLabel"] == "Disable"
    assert prompt["danger"] is True
    message = prompt["message"]
    assert "very next request" in message, "it must say when it takes effect"
    assert "API key" in message, "an API key of theirs stops working too"
    assert "Nothing is deleted" in message and "Enable" in message, "it must say it is reversible"


def test_enabling_a_disabled_account_does_not_ask() -> None:
    """Enable only restores access."""
    assert _prompt({"id": "u-1", "username": "bob", "disabled": True}) is None


@pytest.mark.parametrize("disabled", [None, 0, ""])
def test_an_account_without_a_disabled_flag_is_treated_as_enabled_and_asks(disabled) -> None:
    assert _prompt({"id": "u-1", "username": "bob", "disabled": disabled}) is not None


def test_the_row_asks_before_it_sends_the_patch_and_sends_nothing_when_declined() -> None:
    body = re.search(r"const toggleDisabled = async \(\) => \{[\s\S]*?\n  \};", SRC)
    assert body, "toggleDisabled is gone"
    text = body.group(0)

    ask = re.search(r"const prompt = ADM_toggleConfirm\(user\);\s*if \(prompt && !\(await confirmDialog\(prompt\)\)\) return;", text)
    assert ask, "the handler must ask through the tested function and stop when the answer is no"
    assert ask.start() < text.index("setBusy(true)"), "a declined prompt must not leave the row busy"
    assert ask.start() < text.index('"PATCH"'), "the prompt comes before the request"
