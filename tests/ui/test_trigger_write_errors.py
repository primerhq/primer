"""What a failed webhook-trigger write says (follow-up to PR 491, the A-20 owner rule).

The server now refuses a PUT or a rotate_token on a webhook trigger by anyone but its owner or an admin: a 403 whose ``title`` is "Forbidden" and
whose ``detail`` says WHY. The console's Clear HMAC swallowed the refusal (``catch (_e) { /* ignore */ }``: the secret stayed and nothing said
so), and Rotate token and the Set HMAC dialog showed only the title. ``TR_writeErrorText`` is the one place that picks the words: the server's
explanation first, then its title, then a fallback. Evaluated in V8; the page is driven by ``tests/ui_e2e/test_trigger_secret_refusal_journey.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

TRIGGERS = (Path(__file__).resolve().parents[2] / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")


@pytest.fixture
def words():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    start = TRIGGERS.index("function TR_writeErrorText(")
    ctx.eval(TRIGGERS[start:TRIGGERS.index("\n}\n", start) + len("\n}\n")])
    try:
        yield lambda err, fallback: ctx.eval(f"TR_writeErrorText({json.dumps(err)}, {json.dumps(fallback)})")
    finally:
        ctx.close()


def test_the_servers_explanation_comes_first(words) -> None:
    err = {"message": "Forbidden", "title": "Forbidden", "detail": "Only the trigger's owner or an admin may change its secrets."}
    assert words(err, "Save failed") == "Only the trigger's owner or an admin may change its secrets."


def test_the_title_is_next_and_the_fallback_last(words) -> None:
    assert words({"message": "Forbidden", "title": "Forbidden"}, "Save failed") == "Forbidden"
    assert words({"title": "Conflict"}, "Save failed") == "Conflict"
    assert words({}, "Save failed") == "Save failed"
    assert words(None, "Save failed") == "Save failed"


def test_a_blank_detail_does_not_hide_the_title(words) -> None:
    assert words({"title": "Forbidden", "detail": ""}, "Save failed") == "Forbidden"


def test_the_three_secret_writes_use_it() -> None:
    clear = TRIGGERS[TRIGGERS.index('title: "Clear HMAC secret?"'):][:900]
    assert "catch (_e) { /* ignore */ }" not in clear, "the refusal must not be swallowed"
    assert "setHmacError(TR_writeErrorText(" in clear
    rotate = TRIGGERS[TRIGGERS.index("const rotateToken = async"):][:900]
    assert "TR_writeErrorText(" in rotate
    dialog = TRIGGERS[TRIGGERS.index("function TR_HmacSecretDialog"):][:1500]
    assert "TR_writeErrorText(" in dialog
