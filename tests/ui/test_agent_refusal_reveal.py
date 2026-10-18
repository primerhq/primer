"""A refused save brings its message into view (platform revamp Review Focus 5; review of #779, required fix 3).

The overlay's body scrolls: the edit form is about 1900px tall and the create form about 1400px, with about 620px of it in sight at 1366x768. A refusal draws its
message under its own field, which is often far from the Save button the operator just pressed, so a refusal could be drawn out of sight and read as a button that
does nothing. After EVERY refused save (the checks the form makes before it sends anything, and the server's 422) the form scrolls the FIRST ``role="alert"`` of the
overlay into view and gives the control that message describes the focus; a message with no control (the line above the footer, the tools) is only scrolled to.

``AG_revealRefusal`` (``ui/components/agents.jsx``) is called from an effect after the commit that draws the message. The V8 context has no DOM, so the harness
(``tests/ui/_agent_form_harness.py``) gives the form a fake ``document`` that finds the dialog, its first alert and the control an alert describes IN WHAT THE FORM
DREW, and records what was scrolled to and focused. That the message really ends up inside the visible part of the body is pinned in a real browser by
``tests/ui_e2e/test_agent_overlay_journey.py`` (``to_be_in_viewport``).
"""

from __future__ import annotations

import pytest

from tests.ui._agent_form_harness import FULL, agent_form, calls, click, mount, prop, recorded, reject_next, reveals, type_into

CENTRED = {"block": "center"}
NO_SCROLL = {"preventScroll": True}


@pytest.fixture
def ctx():
    with agent_form() as c:
        yield c


def fill_required(ctx) -> None:
    type_into(ctx, "agent-id", "refund-triage")
    type_into(ctx, "agent-description", "Triages refunds")


def scrolled_to(ctx) -> list[str]:
    """The text of every message the form scrolled into view, in order."""
    return [r["text"] for r in reveals(ctx) if r["call"] == "scrollIntoView"]


def focused(ctx) -> list[str]:
    """The id of every control the form gave the focus, in order."""
    return [r["id"] for r in reveals(ctx) if r["call"] == "focus"]


def test_nothing_is_brought_into_view_until_a_save_is_refused(ctx):
    mount(ctx)
    type_into(ctx, "agent-id", "Bad Name!")
    type_into(ctx, "agent-max-tool-turns", "", bad_input=True)
    assert reveals(ctx) == [], "a message drawn while the operator types is already where they are looking"


# ---- the checks before anything is sent -------------------------------------------------------------------------------------------------------------------


def test_a_refused_description_is_scrolled_to_and_its_field_takes_the_focus(ctx):
    mount(ctx)
    type_into(ctx, "agent-id", "refund-triage")
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []
    assert [r["call"] for r in reveals(ctx)] == ["scrollIntoView", "focus"], "the message is centred in the body, then its control takes the focus"
    assert focused(ctx) == ["na-description"]
    [text] = scrolled_to(ctx)
    assert text.startswith("Describe what this agent is for")
    scroll, focus = reveals(ctx)
    assert scroll["opts"] == CENTRED, "centred, so the field just above the message is in sight with it"
    assert focus["opts"] == NO_SCROLL, "the focus does not scroll the body a second time"


def test_an_edit_that_clears_the_description_is_refused_in_sight(ctx):
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-description", "  ")
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []
    assert focused(ctx) == ["na-description"] and scrolled_to(ctx)[0].startswith("Describe what this agent is for")


def test_the_first_message_of_the_overlay_is_the_one_scrolled_to(ctx):
    """A bad id and a blank description are refused together: the id's message comes first in the overlay."""
    mount(ctx)
    type_into(ctx, "agent-id", "Bad Name!")
    click(ctx, "agent-form-submit")
    assert focused(ctx) == ["na-id"]
    [text] = scrolled_to(ctx)
    assert "lowercase letters, digits, hyphens and underscores" in text, "not the description's, which is later in the overlay"


def test_a_response_format_that_does_not_parse_is_scrolled_to_in_the_reopened_fold(ctx):
    mount(ctx)
    fill_required(ctx)
    type_into(ctx, "agent-response-format", "{")
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []
    assert prop(ctx, "agent-advanced-fold", "data-open") == "true"
    assert focused(ctx) == ["na-response-format"]
    [text] = scrolled_to(ctx)
    assert text.startswith("JSON parse:")


def test_a_number_the_browser_cannot_read_is_scrolled_to_when_save_is_pressed(ctx):
    mount(ctx)
    fill_required(ctx)
    type_into(ctx, "agent-temperature", "", bad_input=True)
    assert reveals(ctx) == []
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []
    assert focused(ctx) == ["na-temperature"]
    assert scrolled_to(ctx) == ["Enter a number, or leave it empty for the model default."]


# ---- the server's 422 --------------------------------------------------------------------------------------------------------------------------------------


def test_a_422_on_a_shown_field_is_scrolled_to_and_its_field_takes_the_focus(ctx):
    reject_next(ctx, {"status": 422, "fieldErrors": [{"loc": ["body", "max_tool_turns"], "msg": "Input should be greater than or equal to 1"}]})
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    assert len(calls(ctx)) == 1, "the request went out and was refused"
    assert focused(ctx) == ["na-max-tool-turns"]
    assert scrolled_to(ctx) == ["Input should be greater than or equal to 1"]


def test_with_several_refusals_from_the_server_the_first_in_the_overlay_is_scrolled_to(ctx):
    reject_next(ctx, {"status": 422, "fieldErrors": [
        {"loc": ["body", "max_output_tokens"], "msg": "too small"}, {"loc": ["body", "temperature"], "msg": "too large"}]})
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    assert focused(ctx) == ["na-temperature"], "Temperature comes before Max output tokens in the overlay, whatever order the server listed them in"
    assert scrolled_to(ctx) == ["too large"]


def test_a_422_on_a_field_of_several_controls_is_scrolled_to_and_nothing_takes_the_focus(ctx):
    """The tools are a picker, not one control: its message is scrolled to, and no control is named by it."""
    reject_next(ctx, {"status": 422, "fieldErrors": [{"loc": ["body", "tools"], "msg": "Unknown toolset nope"}]})
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    assert scrolled_to(ctx) == ["Unknown toolset nope"]
    assert focused(ctx) == []


def test_a_422_on_a_field_the_overlay_does_not_show_is_scrolled_to_above_the_footer(ctx):
    reject_next(ctx, {"status": 422, "fieldErrors": [{"loc": ["body", "harness_id"], "msg": "Extra inputs are not permitted"}]})
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    assert scrolled_to(ctx) == ["harness_id: Extra inputs are not permitted"]
    assert focused(ctx) == [], "the line above the footer describes no control"


def test_a_422_inside_a_fold_is_scrolled_to_once_the_fold_has_opened(ctx):
    reject_next(ctx, {"status": 422, "fieldErrors": [{"loc": ["body", "response_format"], "msg": "not a JSON Schema"}]})
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    assert prop(ctx, "agent-advanced-fold", "data-open") == "true"
    assert focused(ctx) == ["na-response-format"] and scrolled_to(ctx) == ["not a JSON Schema"]


# ---- what does not bring anything into view ----------------------------------------------------------------------------------------------------------------


def test_a_save_that_works_brings_nothing_into_view(ctx):
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    assert len(recorded(ctx)["created"]) == 1
    assert reveals(ctx) == []


def test_a_failure_that_is_not_a_422_is_a_toast_and_brings_nothing_into_view(ctx):
    reject_next(ctx, {"status": 500, "title": "Internal Server Error", "detail": "boom"})
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    assert recorded(ctx)["toasts"][0]["detail"] == "boom"
    assert reveals(ctx) == []


def test_every_refused_save_brings_its_message_into_view_again(ctx):
    """A second press of Save with the same refusal scrolls back to it: the message is the same, the operator may have scrolled away."""
    mount(ctx)
    type_into(ctx, "agent-id", "refund-triage")
    click(ctx, "agent-form-submit")
    click(ctx, "agent-form-submit")
    assert focused(ctx) == ["na-description", "na-description"]
    assert len(scrolled_to(ctx)) == 2
