"""The rebuilt agent overlay: layout, copy, states and footer (platform revamp plan 05, Task 4; design section 9, layout A).

``AG_NewAgentModal`` (``ui/components/agents.jsx``) is one column in the shared platform kit: Id and Model profile in a row, Description full width, the
system prompt as tabs over one big editor, a big compaction box, Temperature, Max tool turns and Max output tokens in one row, the tool picker, and Voice and
Advanced folded. The Autonomous segment is gone (it is per session; the Agent has no such field). Every message the server refuses a save with is drawn
somewhere the operator can see it (Review Focus 5), and an agent on a deleted model profile keeps that profile (Review Focus 2).

The form runs in V8 (``tests/ui/_agent_form_harness.py``) on stand-ins for the kit parts: ``PK_FormOverlay`` draws its title as ``data-title`` and its footer in
two places, ``PK_Field`` its ``help``, ``error``, ``span`` and ``required`` as ``data-*``, and ``PK_Fold`` its ``open`` as ``data-open``. What the real kit does with the
same props is in ``test_agent_overlay_real_kit.py``; ``test_agent_form_body.py`` pins what a save sends.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.ui._agent_form_harness import (
    FULL,
    FULL_SENT,
    MANAGED,
    PROFILE_CHOICES_KEY,
    PROFILE_ROWS,
    agent_form,
    calls,
    click,
    mount,
    options,
    press_escape,
    prop,
    real_answer,
    real_refusal,
    recorded,
    reject_next,
    reject_with,
    set_api,
    set_picker_selection,
    set_resource,
    testids_under,
    text_of,
    texts,
    type_into,
)

SRC = (Path(__file__).resolve().parents[2] / "ui" / "components" / "agents.jsx").read_text(encoding="utf-8")


def function_source(name: str) -> str:
    """The source of the top-level function ``name``: from ``function NAME(`` to the first line that is only a closing brace."""
    start = SRC.index("function " + name + "(")
    return SRC[start:SRC.index("\n}\n", start) + len("\n}\n")]


@pytest.fixture
def ctx():
    with agent_form() as c:
        yield c


def fill_required(ctx) -> None:
    type_into(ctx, "agent-id", "refund-triage")
    type_into(ctx, "agent-description", "Triages refunds")


# ---- the shell ------------------------------------------------------------------------------------------------------------------------------------


def test_the_title_says_what_the_overlay_does():
    for existing, title in ((None, "New agent"), (FULL, "Edit agent"), (MANAGED, "Managed agent")):
        with agent_form() as ctx:
            mount(ctx, existing=existing)
            assert prop(ctx, "agent-form", "data-title") == title
            ids = testids_under(ctx, "agent-form")
            assert ("agent-form-submit" in ids) == (existing is not MANAGED)
            assert ("agent-form-close" in ids) == (existing is MANAGED)


def test_a_managed_agent_is_a_locked_summary_and_close_closes_it():
    with agent_form() as ctx:
        mount(ctx, existing=MANAGED)
        shown = texts(ctx)
        assert "Managed by harness h1" in shown
        assert "Direct edits are blocked. Update the harness's sync or uninstall flow instead." in shown
        assert "refund-triage" in shown  # the locked summary
        assert not [t for t in testids_under(ctx, "agent-form") if t.startswith("field:")]
        assert testids_under(ctx, "overlay-footer-right") == ["agent-form-close"]
        click(ctx, "agent-form-close")
        assert recorded(ctx)["closes"] == 1


def test_cancel_closes_the_overlay(ctx):
    mount(ctx)
    click(ctx, "agent-form-cancel")
    assert recorded(ctx)["closes"] == 1


# ---- the fields -------------------------------------------------------------------------------------------------------------------------------------


def test_the_fields_run_in_the_designs_order(ctx):
    mount(ctx)
    assert [t for t in testids_under(ctx, "overlay-body") if t.startswith("field:")] == [
        "field:Id", "field:Model profile", "field:Description", "field:System prompt", "field:Compaction prompt",
        "field:Temperature", "field:Max tool turns", "field:Max output tokens", "field:Tools",
        "field:Text-to-speech voice", "field:Response format"]


def test_id_and_model_profile_share_a_row_and_the_numbers_share_another(ctx):
    mount(ctx)
    assert [t for t in testids_under(ctx, "grid:2") if t.startswith("field:")] == ["field:Id", "field:Model profile", "field:Description"]
    assert prop(ctx, "field:Description", "data-span") == "full"
    assert [t for t in testids_under(ctx, "grid:3") if t.startswith("field:")] == ["field:Temperature", "field:Max tool turns", "field:Max output tokens"]
    assert [prop(ctx, f"field:{n}", "data-required") for n in ("Id", "Model profile", "Description")] == ["false", "true", "true"]


def test_the_required_controls_say_so_to_assistive_technology(ctx):
    """The kit draws the asterisk as decoration; the control itself is what says it is required."""
    mount(ctx)
    assert prop(ctx, "agent-model-profile", "aria-required") == "true"
    assert prop(ctx, "agent-description", "aria-required") == "true"
    assert prop(ctx, "agent-id", "aria-required") is None


def test_the_helpers_are_the_designs(ctx):
    mount(ctx)
    assert prop(ctx, "field:Model profile", "data-help") == "The default; a run can override it"
    assert prop(ctx, "field:System prompt", "data-help") == "The parts are joined with a blank line when the prompt is built."
    assert prop(ctx, "field:Compaction prompt", "data-help") == "Leave empty to use the default compaction prompt."
    assert prop(ctx, "field:Max output tokens", "data-help") == "Empty means no limit"
    assert prop(ctx, "field:Tools", "data-help") == "Pick single tools, never whole toolsets"
    assert prop(ctx, "field:Response format", "data-help") == "A JSON Schema the answer must match. Leave empty to run the agent unconstrained."


def test_the_placeholders_say_what_an_empty_field_means(ctx):
    mount(ctx)
    assert prop(ctx, "agent-id", "placeholder") == "Optional, e.g. refund-triage"
    assert prop(ctx, "agent-description", "placeholder") == "What this agent is for"
    assert prop(ctx, "agent-temperature", "placeholder") == "Model default"
    assert prop(ctx, "agent-max-tool-turns", "placeholder") == "No limit"
    assert prop(ctx, "agent-max-output-tokens", "placeholder") == "No limit"
    assert prop(ctx, "agent-compaction-prompt", "placeholder") == "How to summarise older turns when the conversation outgrows the context window"


def test_the_id_is_locked_when_editing_and_says_why():
    with agent_form() as ctx:
        mount(ctx)
        assert prop(ctx, "agent-id", "disabled") is False and prop(ctx, "field:Id", "data-help") == ""
    with agent_form() as ctx:
        mount(ctx, existing=FULL)
        assert prop(ctx, "agent-id", "disabled") is True
        assert prop(ctx, "field:Id", "data-help") == "The id cannot change after the agent is created."


def test_the_compaction_box_is_seven_rows_and_resizable(ctx):
    mount(ctx)
    assert prop(ctx, "agent-compaction-prompt", "rows") == 7 and prop(ctx, "agent-compaction-prompt", "style")["resize"] == "vertical"


def test_the_number_fields_are_numbers(ctx):
    mount(ctx)
    assert [prop(ctx, t, "type") for t in ("agent-temperature", "agent-max-tool-turns", "agent-max-output-tokens")] == ["number"] * 3
    assert prop(ctx, "agent-temperature", "id") == "na-temperature"
    assert prop(ctx, "agent-max-tool-turns", "id") == "na-max-tool-turns"
    assert prop(ctx, "agent-max-output-tokens", "id") == "na-max-output-tokens"
    assert prop(ctx, "agent-compaction-prompt", "id") == "na-compaction-prompt"
    assert prop(ctx, "agent-response-format", "id") == "na-response-format"


def test_the_tool_picker_is_always_on_the_page_with_the_agents_tools(ctx):
    mount(ctx, existing=FULL)
    assert prop(ctx, "tool-picker-stub", "data-selected") == '["system__list_files","web__search"]'


def test_there_is_no_autonomous_control(ctx):
    mount(ctx)
    assert not any("utonomous" in t for t in texts(ctx))
    assert "agent-autonomous-segment" not in testids_under(ctx, "agent-form")


def test_voice_is_folded_and_only_there_with_text_to_speech():
    with agent_form(tts_configured=True) as ctx:
        mount(ctx)
        assert prop(ctx, "agent-voice-fold", "data-open") == "false"
    with agent_form(tts_configured=False) as ctx:
        mount(ctx)
        assert "agent-voice-fold" not in testids_under(ctx, "agent-form")


def test_the_voice_keeps_its_default_option_and_its_pairing_note():
    with agent_form() as ctx:
        mount(ctx, existing=FULL)
        assert options(ctx, "agent-tts-voice") == [
            {"value": "", "text": "(use the global default)"}, {"value": "alloy", "text": "alloy"}, {"value": "nova", "text": "nova"}]
        assert prop(ctx, "agent-tts-voice", "value") == "alloy"
        assert text_of(ctx, "agent-voice-pairing-note") == "alloy · pairs with the identity chip"
    with agent_form() as ctx:  # no voice picked: no note
        mount(ctx)
        assert prop(ctx, "agent-tts-voice", "value") == ""
        assert "agent-voice-pairing-note" not in testids_under(ctx, "agent-form")


def test_advanced_is_folded_and_holds_the_three_advanced_controls(ctx):
    mount(ctx)
    assert prop(ctx, "agent-advanced-fold", "data-open") == "false"
    assert {"na-compaction-tool-access", "na-allow-external-tools", "agent-response-format"} <= set(testids_under(ctx, "agent-advanced-fold"))


def test_the_toggles_are_one_line_with_their_help_under_them_and_the_8_round_fallback_is_said(ctx):
    mount(ctx)
    tool_access = (
        "Let the compaction prompt call this agent's tools while it summarises, for example to save the summary to workspace files. "
        "The calls run in a bounded loop (Max tool turns, or 8 rounds when it has no limit) and do not enter the conversation history. "
        "Leave off for text-only compaction.")
    external = (
        "Let API callers attach their own tool definitions when they invoke this agent. When the model calls one, the turn pauses until the "
        "caller answers through the invocation API. Leave off to refuse such calls.")
    assert text_of(ctx, "na-compaction-tool-access-help") == tool_access
    assert text_of(ctx, "na-allow-external-tools-help") == external
    for testid in ("na-compaction-tool-access", "na-allow-external-tools"):
        assert prop(ctx, testid, "role") == "switch"
        described = prop(ctx, testid, "aria-describedby")
        assert described and described == prop(ctx, testid + "-help", "id"), "the switch is described by the help under it"
    assert text_of(ctx, "na-compaction-tool-access-label") == "Tool access during compaction"
    assert text_of(ctx, "na-allow-external-tools-label") == "Allow external tools"


def test_a_toggle_flips_and_says_so(ctx):
    mount(ctx)
    assert prop(ctx, "na-allow-external-tools", "aria-checked") is False
    click(ctx, "na-allow-external-tools")
    assert prop(ctx, "na-allow-external-tools", "aria-checked") is True


def test_escape_leaves_full_screen_before_it_closes_the_form(ctx):  # Review Focus 4, through the whole form
    mount(ctx, existing=FULL)
    click(ctx, "agent-system-prompt-fullscreen")
    press_escape(ctx)
    assert prop(ctx, "agent-system-prompt", "data-fullscreen") == "false" and recorded(ctx)["closes"] == 0
    press_escape(ctx)
    assert recorded(ctx)["closes"] == 1


def test_the_overlay_passes_help_and_error_to_the_kit_and_never_the_inline_hint():
    """Design 6.2: help and errors go UNDER their control. FormField's `hint` is inline label text, and a kit field never takes it."""
    assert "hint=" not in function_source("AG_NewAgentModal")
    assert "FormField" not in function_source("AG_NewAgentModal")


def test_the_rebuilt_overlay_has_no_em_dash():
    for name in ("AG_Toggle", "AG_PromptTabs", "AG_NewAgentModal"):
        assert chr(0x2014) not in function_source(name)


# ---- refusals the operator can see (Review Focus 5) -------------------------------------------------------------------------------------------------


def test_a_bad_response_format_reopens_advanced_and_sends_nothing(ctx):  # Review Focus 5
    mount(ctx)
    fill_required(ctx)
    type_into(ctx, "agent-response-format", "{")
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []
    assert prop(ctx, "agent-advanced-fold", "data-open") == "true"
    assert prop(ctx, "field:Response format", "data-error").startswith("JSON parse:")


# Every refusal below is an envelope the REAL agent router answered (tests/_support/agent_form_server.py), thrown as the real ApiError
# (reject_with). The server says a field in two shapes (audit G3-03): request validation lists the field in extensions.errors, at
# ["body", "<field>"] on the create route and at ["<field>"] on the edit route (it validates the body itself); a refused tool binding or
# profile is {error, field, message} with no list. Both are drawn under the field. The stand-in shapes these tests used to write by hand
# ({"loc": ["body", "tools"]}, a harness_id the Agent model never refuses) hid that the real ones landed above the footer or in a toast.

# A change of FULL the edit route refuses, for each field the overlay shows: the envelope is the router's answer to that body. The form cannot
# send some of them (a prompt part that is a number), so they stand for any refusal of the field; the tests after this one send what the
# form really sends.
SHOWN_FIELD_REFUSALS = {
    "Description": {"description": "  "},
    "Model profile": {"model": {"profile_id": "gone"}},
    "System prompt": {"system_prompt": ["You triage refunds.", 1]},
    "Compaction prompt": {"compaction_prompt": [1]},
    "Temperature": {"temperature": -0.5},
    "Max tool turns": {"max_tool_turns": 0},
    "Max output tokens": {"max_output_tokens": 0},
    "Tools": {"tools": ["ghost__x"]},
    "Text-to-speech voice": {"tts_voice": 5},
    "Response format": {"response_format": "nope"},
}


def server_words(envelope: dict) -> str:
    """The words the router said for its one refused field: the request-validation entry's ``msg``, or a pre-write check's ``message``."""
    ext = envelope["extensions"]
    if "errors" in ext:
        [entry] = ext["errors"]
        return entry["msg"]
    return ext["message"]


@pytest.mark.parametrize("label", sorted(SHOWN_FIELD_REFUSALS))
def test_every_shown_field_shows_the_edit_routes_refusal_under_it(ctx, label):  # Review Focus 5, audit G3-03
    envelope = real_refusal(SHOWN_FIELD_REFUSALS[label])
    reject_with(ctx, envelope)
    mount(ctx, existing=FULL)
    click(ctx, "agent-form-submit")
    assert prop(ctx, "field:" + label, "data-error") == server_words(envelope)
    assert "agent-form-error" not in testids_under(ctx, "agent-form"), "the field has it, not the line above the footer"
    assert recorded(ctx)["toasts"] == []


def test_the_create_routes_refusal_of_the_id_sits_under_the_id(ctx):
    envelope = real_refusal({"id": "Bad Name!"}, create=True)
    reject_with(ctx, envelope)
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    assert prop(ctx, "field:Id", "data-error") == server_words(envelope)


def test_an_edit_refused_for_its_temperature_says_so_under_temperature(ctx):  # audit G3-03 (a)
    sent, envelope = real_answer(lambda c: type_into(c, "agent-temperature", "-0.5"), existing=FULL)
    assert sent == {**FULL_SENT, "temperature": -0.5}
    assert [e["loc"] for e in envelope["extensions"]["errors"]] == [["temperature"]], "the edit route's location has no body prefix"
    reject_with(ctx, envelope)
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-temperature", "-0.5")
    click(ctx, "agent-form-submit")
    assert prop(ctx, "field:Temperature", "data-error") == "Input should be greater than or equal to 0"
    assert "agent-form-error" not in testids_under(ctx, "agent-form")


def test_a_create_refused_for_its_temperature_says_so_under_temperature_too(ctx):
    def drive(c):
        type_into(c, "agent-id", "refund-desk")  # the store holds refund-triage already
        type_into(c, "agent-description", "Answers refund questions")
        type_into(c, "agent-temperature", "-0.5")

    _sent, envelope = real_answer(drive)
    assert [e["loc"] for e in envelope["extensions"]["errors"]] == [["body", "temperature"]]
    reject_with(ctx, envelope)
    mount(ctx)
    drive(ctx)
    click(ctx, "agent-form-submit")
    assert prop(ctx, "field:Temperature", "data-error") == "Input should be greater than or equal to 0"


def test_an_edit_refused_for_its_response_format_reopens_advanced_and_says_so_there(ctx):  # audit G3-03 (a)
    sent, envelope = real_answer(lambda c: type_into(c, "agent-response-format", '"not a schema"'), existing=FULL)
    assert sent["response_format"] == "not a schema", "it parses, so the form sends it"
    reject_with(ctx, envelope)
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-response-format", '"not a schema"')
    assert prop(ctx, "agent-advanced-fold", "data-open") == "false"
    click(ctx, "agent-form-submit")
    assert prop(ctx, "agent-advanced-fold", "data-open") == "true"
    assert prop(ctx, "field:Response format", "data-error") == "Input should be a valid dictionary"


def test_a_tool_binding_the_server_refuses_sits_under_tools_not_in_a_toast(ctx):  # audit G3-03 (b)
    def drive(c):
        type_into(c, "agent-id", "refund-desk")  # the store holds refund-triage already
        type_into(c, "agent-description", "Answers refund questions")
        set_picker_selection(c, ["web__search", "ghost__x"])  # a toolset deleted after the picker loaded
        click(c, "tool-picker-stub")

    _sent, envelope = real_answer(drive)
    assert envelope["extensions"]["error"] == "toolset_not_found" and "errors" not in envelope["extensions"]
    reject_with(ctx, envelope)
    mount(ctx)
    drive(ctx)
    click(ctx, "agent-form-submit")
    assert prop(ctx, "field:Tools", "data-error") == "tools name toolsets that do not exist: 'ghost'; create them first or remove those tools"
    assert recorded(ctx)["toasts"] == []
    assert "agent-form-error" not in testids_under(ctx, "agent-form")


def test_an_edit_that_binds_a_tool_the_server_refuses_says_so_under_tools(ctx):
    def drive(c):
        set_picker_selection(c, ["system__list_files", "web__search", "ghost__x"])
        click(c, "tool-picker-stub")

    _sent, envelope = real_answer(drive, existing=FULL)
    reject_with(ctx, envelope)
    mount(ctx, existing=FULL)
    drive(ctx)
    click(ctx, "agent-form-submit")
    assert prop(ctx, "field:Tools", "data-error").startswith("tools name toolsets that do not exist: 'ghost'")
    assert recorded(ctx)["toasts"] == []


def test_a_refusal_of_a_field_the_overlay_has_no_place_for_is_one_line_above_the_footer(ctx):  # Review Focus 5
    """The switches have no message slot: a refusal of one (the router's answer to a body the form cannot send) is said above the footer."""
    envelope = real_refusal({"allow_external_tools": "maybe"})
    reject_with(ctx, envelope)
    mount(ctx, existing=FULL)
    click(ctx, "agent-form-submit")
    assert text_of(ctx, "agent-form-error") == "allow_external_tools: Input should be a valid boolean, unable to interpret input"
    assert prop(ctx, "agent-form-error", "role") == "alert"
    assert recorded(ctx)["toasts"] == []


def test_a_422_on_a_field_inside_a_fold_opens_the_fold(ctx):
    reject_with(ctx, real_refusal({"response_format": "nope"}))
    mount(ctx, existing=FULL)
    assert prop(ctx, "agent-advanced-fold", "data-open") == "false"
    click(ctx, "agent-form-submit")
    assert prop(ctx, "agent-advanced-fold", "data-open") == "true"
    assert prop(ctx, "field:Response format", "data-error") == "Input should be a valid dictionary"


def test_a_422_on_the_voice_opens_the_voice_fold_and_without_text_to_speech_it_is_a_line_above_the_footer():
    refusal = real_refusal({"tts_voice": 5})
    with agent_form(tts_configured=True) as ctx:
        reject_with(ctx, refusal)
        mount(ctx, existing=FULL)
        click(ctx, "agent-form-submit")
        assert prop(ctx, "agent-voice-fold", "data-open") == "true"
        assert prop(ctx, "field:Text-to-speech voice", "data-error") == "Input should be a valid string"
    with agent_form(tts_configured=False) as ctx:
        reject_with(ctx, refusal)
        mount(ctx, existing=FULL)
        click(ctx, "agent-form-submit")
        assert text_of(ctx, "agent-form-error") == "tts_voice: Input should be a valid string"


def test_a_failure_that_is_not_a_422_is_a_toast_and_no_line(ctx):
    reject_next(ctx, {"status": 500, "title": "Internal Server Error", "detail": "boom"})
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    [toast] = recorded(ctx)["toasts"]
    assert (toast["kind"], toast["title"], toast["detail"]) == ("error", "Internal Server Error", "boom")
    assert "agent-form-error" not in testids_under(ctx, "agent-form")


def test_a_refused_description_and_a_bad_id_sit_under_their_fields_and_send_nothing(ctx):
    mount(ctx)
    type_into(ctx, "agent-id", "Bad Name!")
    click(ctx, "agent-form-submit")
    assert calls(ctx) == []
    assert "lowercase letters, digits, hyphens and underscores" in prop(ctx, "field:Id", "data-error")
    assert "The id cannot change after the agent is created." in prop(ctx, "field:Id", "data-error")
    assert prop(ctx, "field:Description", "data-error").startswith("Describe what this agent is for")
    type_into(ctx, "agent-id", "refund-triage")
    assert prop(ctx, "field:Id", "data-error") == "", "typing in a field takes its message away"


# ---- the model profile (Review Focus 2) -------------------------------------------------------------------------------------------------------------


def test_the_model_profile_is_a_dropdown_of_profiles(ctx):
    mount(ctx)
    assert options(ctx, "agent-model-profile") == [{"value": "fast", "text": "fast"}, {"value": "slow", "text": "slow"}]
    assert prop(ctx, "agent-model-profile", "value") == "fast"
    assert prop(ctx, "agent-model-profile", "id") == "na-model-profile"


def test_picking_another_profile_is_what_is_saved(ctx):
    mount(ctx)
    fill_required(ctx)
    type_into(ctx, "agent-model-profile", "slow")
    click(ctx, "agent-form-submit")
    assert calls(ctx)[0]["body"]["model"] == {"profile_id": "slow"}


def test_a_deleted_profile_stays_selected_and_says_so(ctx):  # Review Focus 2
    mount(ctx, existing={**FULL, "model": {"profile_id": "gone"}})
    assert options(ctx, "agent-model-profile")[0] == {"value": "gone", "text": "gone (missing)"}
    assert prop(ctx, "agent-model-profile", "value") == "gone"
    assert prop(ctx, "field:Model profile", "data-error") == "This profile no longer exists. Pick another one."


def test_the_profile_list_says_when_it_is_loading_empty_or_broken():
    with agent_form() as ctx:
        set_resource(ctx, PROFILE_CHOICES_KEY, {"data": None, "loading": True, "error": None})
        mount(ctx)
        assert options(ctx, "agent-model-profile") == [{"value": "", "text": "Loading model profiles…"}]
        assert prop(ctx, "agent-form-submit", "disabled") is True
    with agent_form() as ctx:
        set_resource(ctx, PROFILE_CHOICES_KEY, {"data": {"items": []}, "loading": False, "error": None})
        mount(ctx)
        assert options(ctx, "agent-model-profile") == [{"value": "", "text": "No model profiles yet"}]
        assert prop(ctx, "field:Model profile", "data-help") == "No model profiles yet. Register an LLM provider and import its models first."
        assert prop(ctx, "agent-form-submit", "disabled") is True
    with agent_form() as ctx:  # a failed load is said as the error, never as "no longer exists"
        set_resource(ctx, PROFILE_CHOICES_KEY, {"data": None, "loading": False, "error": {"detail": "upstream down"}})
        mount(ctx, existing=FULL)
        assert options(ctx, "agent-model-profile") == [{"value": "slow", "text": "slow"}]
        assert prop(ctx, "field:Model profile", "data-error") == "Could not load the model profiles: upstream down"
        assert prop(ctx, "field:Model profile", "data-help") == "The default; a run can override it"


def test_a_failed_profile_load_on_create_is_the_error_and_the_empty_option():
    with agent_form() as ctx:
        set_resource(ctx, PROFILE_CHOICES_KEY, {"data": None, "loading": False, "error": {"message": "network down"}})
        mount(ctx)
        assert options(ctx, "agent-model-profile") == [{"value": "", "text": "No model profiles yet"}]
        assert prop(ctx, "field:Model profile", "data-error") == "Could not load the model profiles: network down"
        assert prop(ctx, "field:Model profile", "data-help") == "The default; a run can override it"


def test_a_profile_the_server_refuses_is_said_under_the_dropdown(ctx):  # audit G3-03 (b)
    """The operator picks a profile the dropdown offers that was deleted on the server after the list loaded."""
    slow_only = [row for row in PROFILE_ROWS if row["id"] == "slow"]
    sent, envelope = real_answer(lambda c: type_into(c, "agent-model-profile", "fast"), existing=FULL, profiles=slow_only)
    assert sent["model"] == {"profile_id": "fast"}
    assert envelope["extensions"]["error"] == "model_profile_not_found" and "errors" not in envelope["extensions"]
    reject_with(ctx, envelope)
    mount(ctx, existing=FULL)
    type_into(ctx, "agent-model-profile", "fast")
    click(ctx, "agent-form-submit")
    assert prop(ctx, "field:Model profile", "data-error") == "ModelProfile 'fast' does not exist; create the profile first or name an existing one"
    assert recorded(ctx)["toasts"] == []


def test_the_servers_refusal_of_the_profile_comes_ahead_of_the_missing_note(ctx):
    reject_with(ctx, real_refusal({"model": {"profile_id": "gone-too"}}))
    mount(ctx, existing={**FULL, "model": {"profile_id": "gone"}})
    assert prop(ctx, "field:Model profile", "data-error") == "This profile no longer exists. Pick another one."
    click(ctx, "agent-form-submit")
    assert prop(ctx, "field:Model profile", "data-error").startswith("ModelProfile 'gone-too' does not exist")


# ---- edit mode and the footer ------------------------------------------------------------------------------------------------------------------------


def test_edit_mode_puts_the_status_first_and_the_sessions_and_raw_config_last(ctx):
    mount(ctx, existing=FULL)
    ids = testids_under(ctx, "overlay-body")
    assert ids.index("agent-status-note") < ids.index("field:Id")
    assert ids.index("agent-advanced-fold") < ids.index("agent-sessions-panel") < ids.index("agent-json-disclosure")


def test_a_new_agent_has_no_status_sessions_or_raw_config(ctx):
    mount(ctx)
    ids = testids_under(ctx, "overlay-body")
    assert not {"agent-status-note", "agent-sessions-panel", "agent-json-disclosure"} & set(ids)


def test_the_footer_has_cancel_on_the_left_and_the_action_on_the_right():
    with agent_form() as ctx:
        mount(ctx)
        assert testids_under(ctx, "overlay-footer-left") == ["agent-form-cancel"]
        assert testids_under(ctx, "overlay-footer-right") == ["agent-form-submit"]
        assert text_of(ctx, "agent-form-submit") == "Create"
        assert prop(ctx, "agent-form-submit", "data-busy") == "false"
        assert prop(ctx, "agent-form-submit", "disabled") is False
    with agent_form() as ctx:
        mount(ctx, existing=FULL)
        assert testids_under(ctx, "overlay-footer-left") == ["agent-form-cancel", "agent-form-delete"]
        assert testids_under(ctx, "overlay-footer-right") == ["agent-form-chat", "agent-form-submit"]
        assert text_of(ctx, "agent-form-submit") == "Save"


def test_delete_and_chat_call_what_the_page_gave_them(ctx):
    mount(ctx, existing=FULL)
    click(ctx, "agent-form-delete")
    click(ctx, "agent-form-chat")
    assert (recorded(ctx)["deletes"], recorded(ctx)["chats"]) == (1, 1)


def test_chat_is_busy_while_the_session_opens_and_says_so():
    with agent_form() as ctx:
        mount(ctx, existing=FULL)
        assert prop(ctx, "agent-form-chat", "data-busy") == "false" and text_of(ctx, "agent-form-chat") == "Chat"
    with agent_form() as ctx:
        mount(ctx, existing=FULL, chat_loading=True)
        assert prop(ctx, "agent-form-chat", "data-busy") == "true" and text_of(ctx, "agent-form-chat") == "Opening chat…"


def test_the_primary_is_busy_while_the_save_is_out_and_says_so():
    with agent_form() as ctx:
        set_api(ctx, "POST", "/agents", {"__pending": True})
        mount(ctx)
        fill_required(ctx)
        click(ctx, "agent-form-submit")
        assert prop(ctx, "agent-form-submit", "data-busy") == "true" and text_of(ctx, "agent-form-submit") == "Creating…"
    with agent_form() as ctx:
        set_api(ctx, "PUT", "/agents/refund-triage", {"__pending": True})
        mount(ctx, existing=FULL)
        click(ctx, "agent-form-submit")
        assert prop(ctx, "agent-form-submit", "data-busy") == "true" and text_of(ctx, "agent-form-submit") == "Saving…"


def test_a_save_that_works_hands_the_row_back_and_closes_nothing_itself(ctx):
    mount(ctx)
    fill_required(ctx)
    click(ctx, "agent-form-submit")
    [row] = recorded(ctx)["created"]
    assert row["id"] == "refund-triage" and recorded(ctx)["closes"] == 0
