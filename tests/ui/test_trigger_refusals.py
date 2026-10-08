"""A refused trigger write says what to do about it, and never shows the code (ticket 01a11bf7-15b7, found while fixing ADM-21; the lead's ruling: a snake_case code is never shown in a banner title, it only picks the remedy text).

Four blocks of ``triggers.jsx`` (the create wizard, the edit dialog, the detail page's Fire now and the subscription dialog) copied the same reading of a failed write: ``envelope.detail`` as
``{code, message}``. The API raises ``HTTPException(detail={code, message})``, but the problem envelope that reaches the browser has ``detail`` as the MESSAGE string and the code in
``extensions.code``, so ``code`` was always null: the banners showed the message only, and the three that compose a title (``Create failed (<code>)``, ``Save failed (<code>)``, ``Fire failed (<code>)``)
never showed a code by accident, not by design. Reading the code correctly would have turned them into ``Create failed (trigger_slug_conflict)``.

* ``TR_refusal(err)`` reads the code where the API puts it (``extensions.code``, then an older ``detail.code``) and the message from the same place, then ``err.detail`` as a string, then the
  error's title, then its message.
* ``TR_refusalText(err, fallback)`` is the banner's detail: the server's own message worked into ONE sentence about what to do, picked by the code (``TR_REMEDIES``, a template per code with
  a ``{message}`` slot: some messages are whole sentences, some are a bare id: ``trigger_not_found`` carries just ``tr-9fab...``). The code itself is never in the text.
* The fire block never read ``err.detail`` at all, so a failed Fire now showed only the HTTP title ("Not Found"); it now shows the server's explanation too.

The table is checked against the router: every code ``primer/api/routers/triggers.py`` raises has a remedy, and a remedy for a code nothing raises is an error, so the table cannot drift.

The two pure functions run in MiniRacer on the real source; the dialogs are JSX, so how they use them is a source check (this checkout has no render harness for them), and
``tests/ui_e2e/test_trigger_refusals_journey.py`` drives the real wizard against the real server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")
ROUTER = (ROOT / "primer" / "api" / "routers" / "triggers.py").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _ctx():
    from py_mini_racer import MiniRacer

    start = SRC.index("var TR_REMEDIES = {")
    end = SRC.index("\n}\n", SRC.index("function TR_refusalText(", start)) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(SRC[start:end])
    return ctx


def _call(expression: str):
    return json.loads(_ctx().eval(f"JSON.stringify({expression})"))


def _text(err, fallback: str = "Request failed") -> str:
    return _call(f"TR_refusalText({json.dumps(err)}, {json.dumps(fallback)})")


# The shapes of real responses (read from a live server): the code is in extensions, `detail` is the message string.
def _envelope(status: int, code: str, message: str) -> dict:
    env = {"type": "/errors/x", "title": "Conflict", "status": status, "detail": message, "instance": "/v1/triggers", "extensions": {"code": code, "message": message, "request_id": "req-1"}}
    return {"envelope": env, "status": status, "title": env["title"], "detail": message, "message": env["title"]}


# The real messages (read from a live server): a slug conflict says "slug 'x' already in use"; a missing trigger carries just its id.
SLUG = _envelope(409, "trigger_slug_conflict", "slug 'nightly' already in use")
NOT_FOUND = _envelope(404, "trigger_not_found", "tr-1")


# ---- reading a refusal ---------------------------------------------------------------------------------------------------------------------------------------


def test_the_code_and_the_message_are_read_from_the_envelope_extensions() -> None:
    assert _call(f"TR_refusal({json.dumps(SLUG)})") == {"code": "trigger_slug_conflict", "message": "slug 'nightly' already in use"}


def test_an_older_envelope_with_the_code_in_a_detail_object_still_reads() -> None:
    err = {"envelope": {"status": 409, "detail": {"code": "trigger_kind_immutable", "message": "cannot change kind"}}}

    assert _call(f"TR_refusal({json.dumps(err)})") == {"code": "trigger_kind_immutable", "message": "cannot change kind"}


@pytest.mark.parametrize(
    "err,expected",
    [
        ({"detail": "plain detail string", "title": "Bad", "message": "Bad"}, "plain detail string"),
        ({"title": "Not Found", "message": "Not Found"}, "Not Found"),
        ({"message": "Failed to fetch"}, "Failed to fetch"),
        ({}, "Request failed"),
        (None, "Request failed"),
    ],
)
def test_without_an_envelope_the_message_falls_back_in_order_and_there_is_no_code(err, expected: str) -> None:
    refusal = _call(f"TR_refusal({json.dumps(err)})")

    assert refusal["code"] is None and refusal["message"] == expected


# ---- the text a banner shows -------------------------------------------------------------------------------------------------------------------------------


def test_a_slug_conflict_is_the_servers_message_then_what_to_do() -> None:
    assert _text(SLUG) == "slug 'nightly' already in use. Choose a different slug."


def test_a_message_that_already_ends_in_a_full_stop_does_not_get_a_second_one() -> None:
    err = _envelope(409, "trigger_kind_immutable", "cannot change kind from 'webhook' to 'delayed'.")

    assert _text(err) == "cannot change kind from 'webhook' to 'delayed'. To change the kind, create a new trigger."


def test_a_missing_trigger_is_a_sentence_not_a_bare_id() -> None:
    """The server's message for it is just the id; the template makes it read as a sentence and says what to do."""
    assert _text(NOT_FOUND) == "Trigger tr-1 was not found: it may have been deleted. Go back to the triggers list."
    assert _text(_envelope(404, "subscription_not_found", "sub-9")) == (
        "Subscription sub-9 was not found: it may have been deleted. Reload the trigger to see its current subscriptions."
    )


def test_the_code_is_never_part_of_the_text() -> None:
    forbidden = _envelope(403, "forbidden_role", "only the trigger's owner or an admin may rotate its webhook token")
    for err, code in ((SLUG, "trigger_slug_conflict"), (NOT_FOUND, "trigger_not_found"), (forbidden, "forbidden_role")):
        assert code not in _text(err), _text(err)


def test_a_cron_the_server_refuses_is_told_to_check_each_fields_range_not_its_field_count() -> None:
    """croniter accepts 5 to 7 fields and @aliases, so \"five fields\" would be wrong advice: what reaches the server is a bad VALUE."""
    text = _text(_envelope(422, "cron_invalid", "invalid cron expression: '0 99 * * *'"))

    assert text == (
        "invalid cron expression: '0 99 * * *'. Check each field's range (minute 0-59, hour 0-23, day of month 1-31, month 1-12, weekday 0-6)."
    )
    assert "five fields" not in text


def test_a_code_with_no_remedy_shows_the_servers_message_alone() -> None:
    assert _text(_envelope(500, "something_new", "the disk is full")) == "the disk is full"


def test_a_failure_with_no_envelope_shows_its_message_or_the_fallback() -> None:
    assert _text({"message": "Failed to fetch"}) == "Failed to fetch"
    assert _text(None, "Fire failed") == "Fire failed"


def test_a_failed_fire_shows_the_servers_explanation_not_just_the_http_title() -> None:
    """The fire block read `err.title` and never `err.detail`, so the banner said "Not Found"."""
    text = _text(NOT_FOUND, "Fire failed")

    assert "Trigger tr-1 was not found" in text and "Go back to the triggers list." in text, text


# ---- the table cannot drift from the router -----------------------------------------------------------------------------------------------------------------------


def _router_codes() -> set[str]:
    return set(re.findall(r'_raise_code\(\s*\d+,\s*"(\w+)"', ROUTER))


def _table_codes() -> set[str]:
    block = SRC[SRC.index("var TR_REMEDIES = {"):]
    block = block[:block.index("\n};")]
    return set(re.findall(r"^  (\w+):", block, re.M))


def test_every_code_the_router_raises_has_a_remedy() -> None:
    missing = _router_codes() - _table_codes()

    assert not missing, f"the router raises {sorted(missing)} but TR_REMEDIES has no sentence for them"


def test_no_remedy_is_for_a_code_nothing_raises() -> None:
    stale = _table_codes() - _router_codes()

    assert not stale, f"TR_REMEDIES has sentences for {sorted(stale)}, which the router no longer raises"


def test_no_remedy_sentence_contains_a_code() -> None:
    for code, sentence in _call("TR_REMEDIES").items():
        assert "_" not in sentence, f"{code}: {sentence!r} reads like code"
        assert sentence.endswith("."), f"{code}: {sentence!r} is a sentence"


# ---- the dialogs use them --------------------------------------------------------------------------------------------------------------------------------------


def test_no_dialog_copies_the_old_reading_of_the_envelope_any_more() -> None:
    assert "const envDetail = env && env.detail;" not in SRC
    assert "envDetail.code" not in SRC


def test_banner_titles_are_plain_and_never_compose_a_code() -> None:
    for composed in ("Create failed (${", "Save failed (${", "Fire failed (${"):
        assert composed not in SRC, f"{composed!r}: a code in a banner title"
    for title in ('title="Create failed"', 'title="Save failed"', 'title="Fire failed"'):
        assert title in SRC, f"{title} is gone"
    # the subscription dialog builds its title from the mode; no banner reads a code at all (the fire-error CHIP reads `error.code ||` from a recorded dispatcher error, a different thing)
    for read in ("submitError.code", "fireError.code", "error.code ?"):
        assert read not in SRC, f"{read!r}: a banner still reads a code"
    assert 'title={`${isEdit ? "Save" : "Create"} failed`}' in SRC


@pytest.mark.parametrize(
    "state_setter,fallback,sites",
    [("setSubmitError", "Request failed", 2), ("setError", "Request failed", 1), ("setFireError", "Fire failed", 1)],
)
def test_each_failed_write_stores_the_text_from_the_one_function(state_setter: str, fallback: str, sites: int) -> None:
    """Counted, not just present: setSubmitError is the create wizard AND the subscription dialog."""
    stored = f'{state_setter}({{ message: TR_refusalText(err, "{fallback}") }});'

    assert SRC.count(stored) == sites, f"{state_setter} must store TR_refusalText(err, {fallback!r}) at {sites} site(s), found {SRC.count(stored)}"


def test_the_writes_that_went_through_the_old_reader_now_go_through_the_one_function() -> None:
    """Rotate token, Clear HMAC and the HMAC dialog call TR_writeErrorText; it must be a delegate of TR_refusalText, not a second reader."""
    body = SRC[SRC.index("function TR_writeErrorText("):]
    body = body[:body.index("\n}\n")]

    assert "return TR_refusalText(err, fallback);" in body, body
    assert "err.detail" not in body and "err.title" not in body, "TR_writeErrorText still reads the error itself"
    for site in ('TR_writeErrorText(err, "Rotate failed")', 'TR_writeErrorText(err, "Could not clear the HMAC secret")', '{ message: TR_writeErrorText(err, "Save failed") }'):
        assert SRC.count(site) == 1, f"{site} should still be written exactly once"


def test_the_delete_modal_and_the_subscription_panel_show_the_refusal_text_not_the_http_title() -> None:
    assert 'title={deleteError.title || "Delete failed"}' not in SRC
    assert 'title="Delete failed"' in SRC and 'detail={TR_refusalText(deleteError, "The trigger could not be deleted.")}' in SRC
    assert 'title={error.title || "Subscription update failed"}' not in SRC
    assert 'title="Subscription update failed"' in SRC and 'detail={TR_refusalText(error, "The subscription could not be updated.")}' in SRC
