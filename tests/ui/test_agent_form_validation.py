"""The agent form says what is wrong before it posts, and the status strip does not print a developer line (ADM-14 and ADM-15 of the 2026-10-08 admin review).

ADM-14: ``Bad Name!`` was accepted as an agent id (``POST /v1/agents`` answers 201; the model has no id pattern), and one press of Create on an empty form made a nameless agent whose
description the form had quietly replaced with ``(no description)``. An id is permanent and sits in URLs and in references, so a typo can only be fixed by delete and recreate, and a
description is how OTHER agents find an agent. The check is client side (the model is not changed); the id is checked when CREATING only (an existing agent's id is locked), the description when creating AND when editing:

* the id is optional (the backend assigns ``agent-<hex>`` when it is blank), but when it is typed it is lowercase letters, digits, hyphens and underscores, starting with a letter or digit,
  at most 63 characters, so it needs no URL escaping and cannot be mistaken for something else; surrounding spaces are not part of it and a whitespace-only id is a blank one (it used to be
  sent as the id ``"  "``, which the server accepts);
* the description is required, on create AND on edit: clearing it on an existing agent used to save the literal "(no description)", and what is sent is the TRIMMED description (the lead's review
  of #566).

The messages go into the form's existing ``fieldErrors`` map under the server's own paths (``body.id``, ``body.description``), so a client refusal and a server 422 share one display, and a
server 422 on ``body.id`` finally has somewhere to show (it had no field).

ADM-15: the strip under "All references resolve" printed ``GET /v1/agents/<id>/status · last checked just now · polled every 30s``: the HTTP verb and path of an endpoint and a poll interval,
and "just now" was a fixed string. It now says that the check runs by itself while the window is open, and keeps the endpoint and interval in a tooltip.

``AG_validateNewAgent`` is pure and runs in MiniRacer on the real source; the form is JSX, so how it uses it is a source check, and ``tests/ui_e2e/test_agent_form_journey.py`` drives the real form.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "ui" / "components" / "agents.jsx").read_text(encoding="utf-8")

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _check(id_: str, description: str) -> dict:
    from py_mini_racer import MiniRacer

    start = SRC.index("function AG_validateNewAgent(")
    end = SRC.index("\n}\n", start) + 3
    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval(SRC[start:end])
    return json.loads(ctx.eval(f"JSON.stringify(AG_validateNewAgent({json.dumps(id_)}, {json.dumps(description)}))"))


# ---- the id ---------------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("id_", ["", "refund-triage", "my_agent_1", "a", "0day", "ag-u0007-ab12cd34", "a" * 63])
def test_a_blank_or_well_formed_id_is_accepted(id_: str) -> None:
    assert "body.id" not in _check(id_, "does a thing")


@pytest.mark.parametrize("id_", ["Bad Name!", "RefundTriage", "refund triage", "-leading", "_leading", "refund/triage", "refund.triage", "naïve", "a" * 64])
def test_an_id_that_would_need_escaping_or_is_ambiguous_is_refused_with_the_rule(id_: str) -> None:
    message = _check(id_, "does a thing")["body.id"]

    assert "lowercase letters, digits, hyphens and underscores" in message
    assert "refund-triage" in message, "the message carries an example"
    assert "cannot change" in message, "and says why it matters now: the name is permanent"


def test_surrounding_spaces_are_not_part_of_the_id() -> None:
    assert "body.id" not in _check("  refund-triage  ", "does a thing")


def test_a_whitespace_only_id_is_a_blank_one_not_an_id_of_spaces() -> None:
    """The form used to send it as the id ``"  "`` and the server accepted it."""
    assert "body.id" not in _check("   ", "does a thing")


# ---- the description ------------------------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("description", ["", "   ", "\n\t "])
def test_a_missing_description_is_refused_with_the_reason(description: str) -> None:
    message = _check("refund-triage", description)["body.description"]

    assert "Describe" in message and "other agents" in message


def test_a_description_is_accepted() -> None:
    assert "body.description" not in _check("refund-triage", "Triages refund requests")


def test_an_empty_form_reports_the_description_and_nothing_about_the_id() -> None:
    """One press of Create on an empty form: the id is optional (the backend assigns one), the description is not."""
    assert list(_check("", "")) == ["body.description"]


def test_both_problems_are_reported_together() -> None:
    assert sorted(_check("Bad Name!", "")) == ["body.description", "body.id"]


# ---- the form -------------------------------------------------------------------------------------------------------------------------------------------------


def _modal() -> str:
    start = SRC.index("function AG_NewAgentModal(")
    return SRC[start:SRC.index("function AG_StatusPanel(", start)]


def test_creating_and_editing_both_check_before_they_post_and_only_creating_checks_the_id() -> None:
    body = _modal()
    submit = body[body.index("const submit = async () => {"):]

    check = re.search(r'const problems = AG_validateNewAgent\(isEdit \? "" : id, description\);\s*if \(Object\.keys\(problems\)\.length\) \{ setFieldErrors\(problems\); return; \}', submit)
    assert check, "both modes stop on a refusal, and an edit passes a blank id so only its description is checked"
    assert "if (!isEdit) {\n      const problems" not in submit, "the check is no longer create-only"
    assert check.start() < submit.index("body = {"), "before the body is built, so nothing is sent"
    assert check.start() < submit.index("await "), "and before any request"


def test_the_description_sent_is_the_trimmed_one_and_no_placeholder_is_ever_substituted() -> None:
    body = _modal()

    assert "description: description.trim()," in body
    assert '"(no description)"' not in body, "a blank description is refused, not replaced by a placeholder that is then saved"


def test_an_edit_that_clears_the_description_is_refused_and_one_that_keeps_it_is_not() -> None:
    """What the edit path passes to the check: a blank id (an existing agent's id is locked), so only the description can be refused."""
    assert list(_check("", "")) == ["body.description"]
    assert _check("", "an existing description") == {}


def test_the_id_sent_is_the_trimmed_one() -> None:
    body = _modal()

    assert "(id.trim() ? { id: id.trim() } : {})" in body, "a whitespace-only id is omitted, so the backend assigns one"
    assert "(id ? { id } : {})" not in body


def test_each_field_shows_its_message_and_drops_it_when_edited() -> None:
    body = _modal()

    for key, field, testid in (("body.id", "id", "na-id-error"), ("body.description", "description", "na-description-error")):
        assert f'fieldErrors["{key}"] && (' in body, f"{key} has a place to show"
        assert f'data-testid="{testid}"' in body
        assert re.search(r"set" + field.capitalize() + r"\(e\.target\.value\);\s*setFieldErrors\(\(m\) => \(\{ \.\.\.m, \"" + re.escape(key) + r"\": undefined \}\)\);", body), f"typing in {field} clears its message"


# ---- ADM-15 ------------------------------------------------------------------------------------------------------------------------------------------------------


def test_the_status_strip_does_not_print_the_endpoint_or_the_poll_interval() -> None:
    panel = SRC[SRC.index("function AG_StatusPanel("):]
    panel = panel[:panel.index("{issues.length > 0 && (")]

    visible = re.sub(r"title=\{`[^`]*`\}|title=\"[^\"]*\"", "", panel)
    assert "GET /v1/agents" not in visible and "polled every" not in visible and "last checked just now" not in visible, "no HTTP verb, path or fixed 'just now' in the visible text"
    assert "Checked automatically while this window is open" in panel
    assert "polled every 30s" in panel, "the technical detail is kept, in a tooltip"
    assert re.search(r'title=\{`GET /v1/agents/\$\{id\}/status, polled every 30s`\}', panel)
