"""What the agent form sends when it saves (platform revamp plan 05, Task 1; design section 9: the rebuilt overlay saves what the form sends today).

A characterization test. Each expected body is derived from the submit code of ``AG_NewAgentModal`` (``ui/components/agents.jsx``), and the overlay
rebuild (plan 05, Task 4) must keep every one of them green UNCHANGED. The form runs in V8 (``tests/ui/_agent_form_harness.py``) with the real
``useMutation``, and the body is read from the recorded ``apiFetch`` call.

Two lossy saves the plan found (its open point 1) are fixed on the lead's call, and the two sections after the first tests pin them: a stored unbounded
``max_tool_turns`` (null) was saved back as 50, and a stored multi-part ``compaction_prompt`` was saved back joined into one part.

The overlay rebuild (Task 4) keeps every test above the "rebuilt overlay" section UNCHANGED and adds the sections below it: a body built through the new
controls, a typed 0 or negative turn cap (still 50, never null), a number the browser cannot read (an error, nothing sent) and a system prompt whose
untouched parts go back exactly as stored.
"""

from __future__ import annotations

import json

import pytest

from tests.ui._agent_form_harness import DEFAULTS, FULL, agent_form, body, calls, click, mount, prop, type_into


@pytest.fixture
def ctx():
    with agent_form() as c:
        yield c


def test_the_full_fixture_sets_every_agent_field():
    from primer.model.agent import Agent

    assert set(FULL) == set(Agent.model_fields)  # a new Agent field fails here until the form carries it


def test_a_new_agent_with_only_the_required_fields_posts_this_body(ctx):
    mount(ctx)
    type_into(ctx, "agent-id", "refund-triage")
    type_into(ctx, "agent-description", "  Triages refunds  ")
    click(ctx, "agent-form-submit")
    [call] = calls(ctx)
    assert (call["method"], call["path"]) == ("POST", "/agents")
    assert call["body"] == {
        "id": "refund-triage", "description": "Triages refunds", "model": {"profile_id": "fast"}, "tools": [],
        "system_prompt": [], "compaction_prompt": [], "compaction_tool_access": False, "allow_external_tools": False,
        "tts_voice": None, "max_tool_turns": 50,
    }


def test_editing_one_field_of_a_fully_set_agent_sends_every_other_field_back_unchanged(ctx):  # plan 05 Review Focus 1
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-description", "Triages refund requests")
    click(ctx, "agent-form-submit")
    [call] = calls(ctx)
    assert (call["method"], call["path"]) == ("PUT", "/agents/refund-triage")
    assert call["body"] == {**{k: v for k, v in FULL.items() if k != "harness_id"}, "description": "Triages refund requests"}


def test_a_voice_set_while_text_to_speech_is_off_is_still_sent_back():
    with agent_form(tts_configured=False) as ctx:
        mount(ctx, existing=FULL)
        click(ctx, "agent-form-submit")
        assert body(ctx)["tts_voice"] == "alloy"


def test_saving_an_agent_with_default_values_sends_them_back(ctx):
    mount(ctx, existing=DEFAULTS)
    click(ctx, "agent-form-submit")
    assert body(ctx) == {
        "id": "plain", "description": "Plain", "model": {"profile_id": "fast"}, "tools": [], "system_prompt": [],
        "compaction_prompt": [], "compaction_tool_access": False, "allow_external_tools": False, "tts_voice": None,
        "max_tool_turns": 50, "max_output_tokens": None, "response_format": None,
    }  # temperature None is left out; the PUT-replace default is None


def test_an_agent_on_a_deleted_profile_keeps_that_profile_on_save(ctx):  # plan 05 Review Focus 2
    mount(ctx, existing={**FULL, "model": {"profile_id": "gone"}})
    click(ctx, "agent-form-submit")
    assert body(ctx)["model"] == {"profile_id": "gone"}


def _attr(ctx, testid: str, name: str):
    """One prop of the element with that testid, as the form rendered it (None when the element has no such prop)."""
    return json.loads(ctx.eval(f"JSON.stringify({{v: MR.find({json.dumps(testid)}).props[{json.dumps(name)}]}})")).get("v")


# ---- an unbounded turn cap (plan 05 open point 1, fixed on the lead's call) ----------------------------------------------------------------------------
# max_tool_turns None means no limit (primer/model/agent.py). The form showed it as 50 and saved 50, so any edit of an unbounded agent capped it.
# A stored None now shows as an empty field that says "No limit", and an empty field saves None.

UNBOUNDED = {**FULL, "max_tool_turns": None}


def test_an_unbounded_turn_cap_left_alone_is_saved_back_unbounded(ctx):
    mount(ctx, existing=UNBOUNDED)
    type_into(ctx, "agent-description", "Triages refund requests")
    click(ctx, "agent-form-submit")
    assert body(ctx) == {**{k: v for k, v in UNBOUNDED.items() if k != "harness_id"}, "description": "Triages refund requests"}


def test_an_unbounded_turn_cap_shows_as_an_empty_field_that_says_no_limit(ctx):
    mount(ctx, existing=UNBOUNDED)
    assert _attr(ctx, "agent-max-tool-turns", "value") == ""
    assert _attr(ctx, "agent-max-tool-turns", "placeholder") == "No limit"


def test_a_number_typed_into_an_unbounded_turn_cap_is_saved(ctx):
    mount(ctx, existing=UNBOUNDED)
    type_into(ctx, "agent-max-tool-turns", "30")
    click(ctx, "agent-form-submit")
    assert body(ctx)["max_tool_turns"] == 30


def test_clearing_a_turn_cap_saves_no_limit(ctx):
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-max-tool-turns", "")
    click(ctx, "agent-form-submit")
    assert body(ctx)["max_tool_turns"] is None


def test_a_new_agent_starts_at_the_default_turn_cap_and_can_be_cleared_to_no_limit(ctx):
    mount(ctx)
    assert _attr(ctx, "agent-max-tool-turns", "value") == "50"
    type_into(ctx, "agent-id", "refund-triage")
    type_into(ctx, "agent-description", "Triages refunds")
    type_into(ctx, "agent-max-tool-turns", "")
    click(ctx, "agent-form-submit")
    assert body(ctx)["max_tool_turns"] is None


# ---- a multi-part compaction prompt (plan 05 open point 1, fixed on the lead's call) --------------------------------------------------------------
# compaction_prompt is a list of parts the runtime joins (primer/model/agent.py). The form shows the parts as one text, joined with a blank line,
# and saved that text as ONE part, so any edit of the agent rewrote a stored multi-part prompt. Saved with the text it showed, the stored parts
# now go back unchanged. An edited text is saved as one part, as before.

PARTS = ["Keep sources.", "Keep the file under edit."]
MULTI_PART = {**FULL, "compaction_prompt": PARTS}


def test_a_multi_part_compaction_prompt_left_alone_is_saved_back_as_its_parts(ctx):
    mount(ctx, existing=MULTI_PART)
    type_into(ctx, "agent-description", "Triages refund requests")
    click(ctx, "agent-form-submit")
    assert body(ctx) == {**{k: v for k, v in MULTI_PART.items() if k != "harness_id"}, "description": "Triages refund requests"}


def test_a_multi_part_compaction_prompt_shows_as_one_text_with_a_blank_line_between_the_parts(ctx):
    mount(ctx, existing=MULTI_PART)
    assert _attr(ctx, "agent-compaction-prompt", "value") == "Keep sources.\n\nKeep the file under edit."


def test_an_edited_compaction_prompt_is_saved_as_one_part(ctx):
    mount(ctx, existing=MULTI_PART)
    type_into(ctx, "agent-compaction-prompt", "Keep sources and the file under edit.")
    click(ctx, "agent-form-submit")
    assert body(ctx)["compaction_prompt"] == ["Keep sources and the file under edit."]


def test_an_edit_typed_back_to_the_text_it_showed_keeps_the_parts(ctx):
    """What decides is the text, not a key press: an edit the operator undoes by hand rewrites nothing."""
    mount(ctx, existing=MULTI_PART)
    type_into(ctx, "agent-compaction-prompt", "Keep sources.")
    type_into(ctx, "agent-compaction-prompt", "Keep sources.\n\nKeep the file under edit.")
    click(ctx, "agent-form-submit")
    assert body(ctx)["compaction_prompt"] == PARTS


def test_a_cleared_compaction_prompt_is_saved_empty(ctx):
    mount(ctx, existing=MULTI_PART)
    type_into(ctx, "agent-compaction-prompt", "")
    click(ctx, "agent-form-submit")
    assert body(ctx)["compaction_prompt"] == []


# ---- the rebuilt overlay (plan 05, Task 4) -------------------------------------------------------------------------------------------------------------


def test_a_new_agent_with_every_field_set_through_the_new_controls_posts_this_body(ctx):
    mount(ctx)
    type_into(ctx, "agent-id", "refund-triage")
    type_into(ctx, "agent-description", "Triages refunds")
    type_into(ctx, "agent-model-profile", "slow")
    type_into(ctx, "agent-temperature", "0.7")
    type_into(ctx, "agent-max-tool-turns", "25")
    type_into(ctx, "agent-max-output-tokens", "4096")
    type_into(ctx, "agent-system-prompt-editor", "You triage refunds.")
    click(ctx, "agent-system-prompt-add")
    type_into(ctx, "agent-system-prompt-editor", "Be brief.")
    click(ctx, "agent-system-prompt-add")  # a third part, left blank
    type_into(ctx, "agent-compaction-prompt", "Keep the cited sources.")
    click(ctx, "tool-picker-stub")
    click(ctx, "na-compaction-tool-access")
    click(ctx, "na-allow-external-tools")
    type_into(ctx, "agent-response-format", '{"type": "object"}')
    type_into(ctx, "agent-tts-voice", "alloy")
    click(ctx, "agent-form-submit")
    assert body(ctx) == {
        "id": "refund-triage", "description": "Triages refunds", "model": {"profile_id": "slow"},
        "tools": ["system__list_files", "web__search"], "system_prompt": ["You triage refunds.", "Be brief."],
        "compaction_prompt": ["Keep the cited sources."], "compaction_tool_access": True, "allow_external_tools": True,
        "tts_voice": "alloy", "temperature": 0.7, "max_tool_turns": 25, "max_output_tokens": 4096,
        "response_format": {"type": "object"},
    }


# ---- a typed 0 or negative turn cap (lead notes on #755, review item a) ------------------------------------------------------------------------------
# An EMPTY field is no limit. A number that is not positive cannot be a cap: it falls back to the server default, 50, and never to null (no limit).
# Nor can a number that rounds down to under 1 (audit G3-03 c): "0.5" was floored to 0 and sent, a body the server refuses (PositiveInt).


@pytest.mark.parametrize("typed", ["0", "-1", "-25", "0.5", "0.99", "-0.3"])
def test_a_typed_zero_or_negative_turn_cap_saves_the_default_not_no_limit(ctx, typed):
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-max-tool-turns", typed)
    click(ctx, "agent-form-submit")
    assert body(ctx)["max_tool_turns"] == 50


# ---- a number the browser cannot read (lead notes on #755, review item c) ----------------------------------------------------------------------------
# A number input reports "" for text it cannot read (validity.badInput), and "" means no limit: the field used to save that. It is an error now, under the
# field, and nothing is sent until the operator fixes or clears it. The same holds for Max output tokens (empty is no limit) and Temperature (empty is the
# model's default), which would otherwise lose a value the same way.

NOT_A_NUMBER = {
    "agent-max-tool-turns": ("Max tool turns", "Enter a whole number, or leave it empty for no limit."),
    "agent-max-output-tokens": ("Max output tokens", "Enter a whole number, or leave it empty for no limit."),
    "agent-temperature": ("Temperature", "Enter a number, or leave it empty for the model default."),
}


@pytest.mark.parametrize("testid", list(NOT_A_NUMBER))
def test_a_number_the_browser_cannot_read_is_an_error_and_sends_nothing(ctx, testid):
    label, message = NOT_A_NUMBER[testid]
    mount(ctx, existing=FULL)
    type_into(ctx, testid, "", bad_input=True)
    assert prop(ctx, f"field:{label}", "data-error") == message
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []


@pytest.mark.parametrize("testid", list(NOT_A_NUMBER))
def test_the_error_goes_when_the_number_can_be_read_again(ctx, testid):
    label, _message = NOT_A_NUMBER[testid]
    mount(ctx, existing=FULL)
    type_into(ctx, testid, "", bad_input=True)
    type_into(ctx, testid, "30")
    assert prop(ctx, f"field:{label}", "data-error") == ""
    click(ctx, "agent-form-submit")
    assert len(calls(ctx)) == 1


@pytest.mark.parametrize("testid", list(NOT_A_NUMBER))
def test_the_input_event_hears_text_typed_into_an_empty_number_field(ctx, testid):
    """React sends no change for an input whose value stays "" (the browser reads unreadable text as ""), so a first character like "e" typed into an
    EMPTY field reaches only the native input event. Found in a real browser: with onChange alone the error never showed and the save went out."""
    label, message = NOT_A_NUMBER[testid]
    mount(ctx)
    type_into(ctx, "agent-id", "refund-triage")
    type_into(ctx, "agent-description", "Triages refunds")
    type_into(ctx, testid, "")
    type_into(ctx, testid, "", bad_input=True, event="input")
    assert prop(ctx, f"field:{label}", "data-error") == message
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []
    type_into(ctx, testid, "", event="input")  # the "e" is deleted: the field is empty and readable again
    assert prop(ctx, f"field:{label}", "data-error") == ""
    click(ctx, "agent-form-submit")
    assert len(calls(ctx)) == 1


def test_a_fraction_above_one_turn_cap_rounds_down(ctx):
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-max-tool-turns", "1.9")
    click(ctx, "agent-form-submit")
    assert body(ctx)["max_tool_turns"] == 1


def test_a_cleared_number_field_is_still_no_limit_not_an_error(ctx):
    """Cleared by hand the value is "" and badInput is false: that is the empty field, which means no limit."""
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-max-output-tokens", "")
    click(ctx, "agent-form-submit")
    assert body(ctx)["max_output_tokens"] is None


# ---- a typed 0 or negative Max output tokens (review of #779, nit 3) ------------------------------------------------------------------------------------
# An EMPTY Max output tokens is no limit, and a typed 0 or negative number was read as "no limit" too: the field saved null without a word, the opposite of what
# was typed. It is an error under the field now, the message the field says for text the browser cannot read, and nothing is sent until the operator types a number
# of 1 or more or clears the field. Max tool turns keeps its own rule (above): a typed 0 or negative saves the server default, 50, and says nothing.

OUTPUT_TOKENS_MESSAGE = "Enter a whole number, or leave it empty for no limit."


@pytest.mark.parametrize("typed", ["0", "-1", "-25", "0.5", "-0.3"])
def test_a_typed_zero_or_negative_output_token_limit_is_an_error_and_sends_nothing(ctx, typed):
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-max-output-tokens", typed)
    assert prop(ctx, "field:Max output tokens", "data-error") == OUTPUT_TOKENS_MESSAGE
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []


@pytest.mark.parametrize("typed", ["0", "-1"])
def test_the_input_event_hears_a_zero_or_negative_output_token_limit_too(ctx, typed):
    """The native input event reaches the field first when React sends no change; the rule is the same on both."""
    mount(ctx)
    type_into(ctx, "agent-id", "refund-triage")
    type_into(ctx, "agent-description", "Triages refunds")
    type_into(ctx, "agent-max-output-tokens", typed, event="input")
    assert prop(ctx, "field:Max output tokens", "data-error") == OUTPUT_TOKENS_MESSAGE
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []


def test_the_output_token_error_goes_when_the_limit_is_a_positive_number(ctx):
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-max-output-tokens", "0")
    type_into(ctx, "agent-max-output-tokens", "512")
    assert prop(ctx, "field:Max output tokens", "data-error") == ""
    click(ctx, "agent-form-submit")
    assert body(ctx)["max_output_tokens"] == 512


def test_the_output_token_error_goes_when_the_field_is_cleared_and_a_cleared_field_is_no_limit(ctx):
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-max-output-tokens", "-4")
    assert prop(ctx, "field:Max output tokens", "data-error") == OUTPUT_TOKENS_MESSAGE
    type_into(ctx, "agent-max-output-tokens", "")
    assert prop(ctx, "field:Max output tokens", "data-error") == ""
    click(ctx, "agent-form-submit")
    assert body(ctx)["max_output_tokens"] is None


def test_the_smallest_limit_is_one_and_a_fraction_above_one_rounds_down(ctx):
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-max-output-tokens", "1")
    assert prop(ctx, "field:Max output tokens", "data-error") == ""
    type_into(ctx, "agent-max-output-tokens", "1.9")
    assert prop(ctx, "field:Max output tokens", "data-error") == ""
    click(ctx, "agent-form-submit")
    assert body(ctx)["max_output_tokens"] == 1


def test_a_zero_or_negative_output_token_limit_leaves_the_other_number_fields_alone(ctx):
    """Max tool turns and Temperature have their own rules; only the limit that has no default to fall back to refuses a value under 1."""
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-max-output-tokens", "0")
    type_into(ctx, "agent-max-tool-turns", "0")
    type_into(ctx, "agent-temperature", "0")
    assert prop(ctx, "field:Max tool turns", "data-error") == "" and prop(ctx, "field:Temperature", "data-error") == ""
    assert prop(ctx, "field:Max output tokens", "data-error") == OUTPUT_TOKENS_MESSAGE


# ---- system prompt parts nobody touched (lead notes on #755, review item d) ---------------------------------------------------------------------------
# Every save trimmed the parts and dropped the blank ones, so a stored part with spaces around it, or an empty one, was rewritten by a save that was about
# something else. A part the operator did not touch goes back exactly as it was stored; a part they typed is trimmed, and one left blank is dropped.

SPACED = ["  Keep the leading spaces.  ", "", "Be brief.\n"]


def test_system_prompt_parts_nobody_touched_are_saved_back_exactly(ctx):
    mount(ctx, existing={**FULL, "system_prompt": SPACED})
    type_into(ctx, "agent-description", "Triages refund requests")
    click(ctx, "agent-form-submit")
    assert body(ctx)["system_prompt"] == SPACED


def test_an_edited_part_is_trimmed_and_the_parts_nobody_touched_stay_as_stored(ctx):
    mount(ctx, existing={**FULL, "system_prompt": ["  Keep the leading spaces.  ", "Be brief."]})
    click(ctx, "agent-system-prompt-tab-1")
    type_into(ctx, "agent-system-prompt-editor", "  Be brief, and cite sources.  ")
    click(ctx, "agent-form-submit")
    assert body(ctx)["system_prompt"] == ["  Keep the leading spaces.  ", "Be brief, and cite sources."]


def test_removing_a_part_keeps_the_others_as_stored(ctx):
    mount(ctx, existing={**FULL, "system_prompt": [" A ", " B ", " C "]})
    click(ctx, "agent-system-prompt-tab-1")
    click(ctx, "agent-system-prompt-remove")
    click(ctx, "agent-form-submit")
    assert body(ctx)["system_prompt"] == [" A ", " C "]


def test_a_part_added_and_left_blank_is_dropped_so_the_array_has_no_hole(ctx):
    mount(ctx, existing={**FULL, "system_prompt": [" A "]})
    click(ctx, "agent-system-prompt-add")
    type_into(ctx, "agent-system-prompt-editor", "   ")
    click(ctx, "agent-form-submit")
    assert body(ctx)["system_prompt"] == [" A "]


def test_a_part_typed_on_a_new_agent_is_trimmed(ctx):
    mount(ctx)
    type_into(ctx, "agent-id", "refund-triage")
    type_into(ctx, "agent-description", "Triages refunds")
    type_into(ctx, "agent-system-prompt-editor", "  You triage refunds.\n\n")
    click(ctx, "agent-form-submit")
    assert body(ctx)["system_prompt"] == ["You triage refunds."]


def test_a_stored_part_typed_twice_excuses_only_the_first(ctx):
    """A stored part is excused once: a second copy the operator typed is theirs, so it is trimmed."""
    mount(ctx, existing={**FULL, "system_prompt": [" A "]})
    click(ctx, "agent-system-prompt-add")
    type_into(ctx, "agent-system-prompt-editor", " A ")
    click(ctx, "agent-form-submit")
    assert body(ctx)["system_prompt"] == [" A ", "A"]
