"""The create-trigger wizard says what is wrong with a schedule where it was entered (ADM-21 and ADM-22 of the 2026-10-08 admin review).

ADM-21: a scheduled trigger's cron was not checked on step 2. ``bad cron`` was accepted, the operator named the trigger on step 3 and pressed Create, and only then did the server
answer 422 ``cron_invalid``; the wizard showed a generic "Create failed" banner on step 3, where the cron field cannot be edited, and left them to guess that Back was the way out. (The
code was not even read: the wizard looked for it in ``envelope.detail``, and for this refusal ``detail`` is a plain string, the code being in ``envelope.extensions.code``.)

* ``TR_cronShapeError(expr)`` is a CONSERVATIVE local check run when leaving step 2: croniter, the server's validator, accepts the aliases (``@daily``, ``@hourly``, ...) and 5 to 7
  space-separated fields, so anything else is certainly refused and can be said at once. It must never refuse what the server accepts (checked below against the real croniter);
  whether the VALUES are valid (a 60 in the minute field) stays the server's call.
* ``TR_scheduleFault(err)`` reads a failed create: a ``cron_invalid`` or ``timezone_invalid`` refusal returns the wizard to step 2 with the message under the offending field and
  everything typed on step 3 kept; any other failure (a slug conflict) stays a banner on step 3.

ADM-22: the slug hint was JSX text ``^[a-z][a-z0-9-]{1,63}$``, where ``{1,63}`` is a JSX expression that evaluates to ``63``, and the label's uppercase turned the rest into
``^[A-Z][A-Z0-9-]63$``: wrong and unreadable. It now says the rule in words.

The two pure functions run in MiniRacer on the real source; the component is JSX, so how it uses them is a source check (this checkout has no render harness), and
``tests/ui_e2e/test_trigger_wizard_schedule_journey.py`` drives the real wizard against the real server.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "triggers.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _call(expression: str):
    from py_mini_racer import MiniRacer

    start = SRC.index("function TR_cronShapeError(")
    end = SRC.index("\n}\n", SRC.index("function TR_scheduleFault(", start)) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = {};")
    ctx.eval((ROOT / "ui" / "foundation" / "api.js").read_text(encoding="utf-8"))   # TR_scheduleFault reads the code through the one reader
    ctx.eval(SRC[start:end])
    return json.loads(ctx.eval(f"JSON.stringify({expression})"))


def _shape(expr: str) -> str:
    return _call(f"TR_cronShapeError({json.dumps(expr)})")


# ---- the shape check -------------------------------------------------------------------------------------------------------------------------------------------------

# What croniter, the server's validator, accepts. The shape check must say nothing about any of them.
ACCEPTED = [
    "0 * * * *", "*/5 * * * *", "0,15 * * * *", "0 9 * * 1-5", "5 4 * * sun", "0 0 * * MON-FRI", "0 0 L * *", "0 0 * * 1#2", "0 0 31 2 *",
    "@daily", "@hourly", "@weekly", "@monthly", "@yearly", "@annually", "* * * * * *", "* * * * * * *", "  0 * * * *  ", "0   9  * *  1",
]
# Right shape, wrong values: the server's call, and it comes back as cron_invalid (handled by TR_scheduleFault).
WRONG_VALUES = ["60 * * * *", "H * * * *", "@reboot", "* * 32 * *"]
WRONG_SHAPE = ["bad cron", "* * *", "* * * *", "* * * * * * * *", "every day", "9am"]


@pytest.mark.parametrize("expr", ACCEPTED + WRONG_VALUES)
def test_the_shape_check_says_nothing_about_a_cron_with_a_plausible_shape(expr: str) -> None:
    assert _shape(expr) == ""


@pytest.mark.parametrize("expr", WRONG_SHAPE)
def test_a_cron_with_the_wrong_number_of_fields_is_named_with_an_example(expr: str) -> None:
    message = _shape(expr)

    assert "5 fields" in message and "minute" in message and "0 9 * * 1" in message
    assert f"This one has {len(expr.split())} " in message, message


def test_an_empty_cron_asks_for_one() -> None:
    assert _shape("   ").startswith("Enter a cron expression")


@pytest.mark.parametrize("expr", ACCEPTED + WRONG_VALUES + WRONG_SHAPE)
def test_the_shape_check_never_refuses_what_the_real_croniter_accepts(expr: str) -> None:
    """The property that makes a LOCAL check safe: whenever the server's validator says yes, this says nothing."""
    from croniter import croniter

    if croniter.is_valid(expr):
        assert _shape(expr) == "", f"croniter accepts {expr!r} but the wizard would refuse it: {_shape(expr)!r}"


def test_the_premise_croniter_really_refuses_the_wrong_shapes_the_check_names() -> None:
    from croniter import croniter

    assert not any(croniter.is_valid(e) for e in WRONG_SHAPE)


# ---- reading a refused create ---------------------------------------------------------------------------------------------------------------------------------------


def _fault(envelope: dict | None, detail="", message="") -> dict | None:
    err = {"envelope": envelope, "detail": detail, "message": message}
    return _call(f"TR_scheduleFault({json.dumps(err)})")


# The shape of the real response (copied from POST /v1/triggers with cron "bad cron" on a live server): the code is in extensions, detail is a string.
REAL_CRON = {
    "type": "/errors/validation-error", "title": "Validation Error", "status": 422, "detail": "invalid cron expression: 'bad cron'",
    "instance": "/v1/triggers", "extensions": {"code": "cron_invalid", "message": "invalid cron expression: 'bad cron'", "request_id": "req-1"},
}
REAL_TZ = {
    "type": "/errors/validation-error", "title": "Validation Error", "status": 422, "detail": "invalid timezone: 'Mars/Base'",
    "instance": "/v1/triggers", "extensions": {"code": "timezone_invalid", "message": "invalid timezone: 'Mars/Base'", "request_id": "req-2"},
}
REAL_SLUG = {
    "type": "/errors/conflict", "title": "Conflict", "status": 409, "detail": "trigger slug 'x' already exists",
    "instance": "/v1/triggers", "extensions": {"code": "trigger_slug_conflict", "message": "trigger slug 'x' already exists", "request_id": "req-3"},
}


def test_a_refused_cron_is_read_from_the_envelope_extensions() -> None:
    assert _fault(REAL_CRON, detail=REAL_CRON["detail"]) == {"field": "cron", "message": "invalid cron expression: 'bad cron'"}


def test_a_refused_timezone_is_read_the_same_way() -> None:
    assert _fault(REAL_TZ, detail=REAL_TZ["detail"]) == {"field": "timezone", "message": "invalid timezone: 'Mars/Base'"}


def test_any_other_refusal_is_not_a_schedule_fault() -> None:
    assert _fault(REAL_SLUG, detail=REAL_SLUG["detail"]) is None
    assert _fault({"status": 500, "detail": "boom"}, detail="boom") is None
    assert _fault(None) is None


def test_an_older_envelope_with_the_code_in_a_detail_object_still_reads() -> None:
    env = {"status": 422, "detail": {"code": "cron_invalid", "message": "invalid cron expression: 'x'"}}

    assert _fault(env) == {"field": "cron", "message": "invalid cron expression: 'x'"}


def test_a_failure_with_no_envelope_at_all_is_not_a_schedule_fault() -> None:
    assert _call("TR_scheduleFault(null)") is None
    assert _call("TR_scheduleFault(new Error('Failed to fetch'))") is None


# ---- the component uses them ---------------------------------------------------------------------------------------------------------------------------------


def _dialog() -> str:
    start = SRC.index("function TR_CreateTriggerDialog(")
    return SRC[start:SRC.index("// TR_TriggerEditDialog", start)]


def test_next_on_the_schedule_step_checks_the_shape_before_it_goes_on() -> None:
    body = _dialog()

    go = re.search(r"const goNext = \(\) => \{[\s\S]*?\n  \};", body)
    assert go, "the wizard's Next goes through goNext"
    text = go.group(0)
    check = re.search(r'if \(step === 2 && kind === "scheduled"\) \{\s*const shape = TR_cronShapeError\(cron\);\s*if \(shape\) \{ setScheduleError\(\{ field: "cron", message: shape \}\); return; \}\s*\}', text)
    assert check, "a scheduled trigger's cron is checked when leaving step 2, and a refusal stays on step 2"
    assert check.start() < text.index("setStep(step + 1)")
    assert "onClick={goNext}" in body


def test_a_refused_schedule_returns_to_step_two_with_step_three_kept() -> None:
    body = _dialog()
    submit = body[body.index("const submit = async"):body.index("const stepTitle")]

    catch = submit[submit.index("catch (err)"):]
    fault = re.search(r"const fault = TR_scheduleFault\(err\);\s*if \(fault\) \{\s*setScheduleError\(fault\);\s*setStep\(2\);\s*return;\s*\}", catch)
    assert fault, "cron_invalid and timezone_invalid go back to the schedule step"
    assert fault.start() < catch.index("setSubmitError("), "any other failure stays a banner on step 3"
    assert "setSlug(" not in catch and "setName(" not in catch, "what was typed on step 3 is kept"


def test_the_message_sits_under_the_field_it_is_about_and_goes_when_it_is_edited() -> None:
    body = _dialog()

    for field, testid, setter in (("cron", "tr-cron-error", "setCron"), ("timezone", "tr-timezone-error", "setTimezone")):
        assert f'scheduleError && scheduleError.field === "{field}"' in body, f"the {field} message is conditional on its own field"
        assert f'data-testid="{testid}"' in body
        assert re.search(setter + r"\(e\.target\.value\); setScheduleError\(null\);", body), f"editing the {field} field clears its message"


# ---- ADM-22: the slug hint ---------------------------------------------------------------------------------------------------------------------------------------------


def test_the_slug_hint_states_the_rule_in_words_not_as_a_regex_that_jsx_swallows() -> None:
    body = _dialog()

    label = re.search(r'<label className="field-label" htmlFor="tr-slug">[\s\S]*?</label>', body)
    assert label, "the slug label is gone"
    hint = re.search(r'<span className="hint"[^>]*>([^<]*)</span>', label.group(0))
    assert hint, "the slug label has its hint"
    assert "{1,63}" not in hint.group(1) and "^[a-z]" not in hint.group(1), "a JSX text node cannot hold a quantifier: it evaluates"
    assert "lowercase letters, digits and hyphens" in hint.group(1) and "2 to 64" in hint.group(1)
    assert "textTransform" in label.group(0), "the label's uppercase must not turn the rule into capital letters"
