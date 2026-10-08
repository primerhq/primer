"""What a failed webhook-trigger write says (follow-up to PR 491, the A-20 owner rule).

The server now refuses a PUT or a rotate_token on a webhook trigger by anyone but its owner or an admin: a 403 whose ``title`` is "Forbidden" and
whose ``detail`` says WHY. The console's Clear HMAC swallowed the refusal (``catch (_e) { /* ignore */ }``: the secret stayed and nothing said
so), and Rotate token and the Set HMAC dialog showed only the title. ``TR_writeErrorText`` is the one place that picks the words: the server's
explanation first, then its title, then a fallback. It now delegates to ``TR_refusalText`` (the #572 review: one reader for every trigger write error), so the V8
context loads that whole chain. Evaluated in V8; the page is driven by ``tests/ui_e2e/test_trigger_secret_refusal_journey.py``.
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
    end = TRIGGERS.index("\n}\n", TRIGGERS.index("function TR_refusalText(", start)) + len("\n}\n")
    ctx.eval(TRIGGERS[start:end])
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


def _between(start: str, end: str) -> str:
    """The source from ``start`` to the next ``end`` after it; both must exist, so a moved anchor fails loudly instead of slicing nothing."""
    i = TRIGGERS.index(start)
    return TRIGGERS[i:TRIGGERS.index(end, i)]


def test_the_three_secret_writes_use_it() -> None:
    clear = _between('title: "Clear HMAC secret?"', 'data-testid="clear-hmac-btn"')
    assert "catch (_e) { /* ignore */ }" not in clear, "the refusal must not be swallowed"
    assert "setHmacError(TR_writeErrorText(" in clear
    rotate = _between("const rotateToken = async", "if (detail.loading && !detail.data)")
    assert "TR_writeErrorText(" in rotate
    dialog = _between("function TR_HmacSecretDialog", "window.TR_HmacSecretDialog")
    assert "TR_writeErrorText(" in dialog
