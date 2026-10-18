"""What the agent form sends when it saves (platform revamp plan 05, Task 1; design section 9: the rebuilt overlay saves what the form sends today).

A characterization test. Each expected body is derived from the submit code of ``AG_NewAgentModal`` (``ui/components/agents.jsx``) as it is, and
the overlay rebuild (plan 05, Task 4) must keep every one of them green UNCHANGED. The form runs in V8 (``tests/ui/_agent_form_harness.py``) with the
real ``useMutation``, and the body is read from the recorded ``apiFetch`` call.
"""

from __future__ import annotations

import pytest

from tests.ui._agent_form_harness import DEFAULTS, FULL, agent_form, body, calls, click, mount, type_into


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


def test_known_quirks_kept_as_today(ctx):
    """Kept on purpose (design 9: same body). The lead decides whether to fix them (plan 05, open point 1); flip this test in the task that does."""
    mount(ctx, existing={**FULL, "compaction_prompt": ["Keep sources.", "Keep the file under edit."], "max_tool_turns": None})
    click(ctx, "agent-form-submit")
    assert body(ctx)["compaction_prompt"] == ["Keep sources.\n\nKeep the file under edit."]
    assert body(ctx)["max_tool_turns"] == 50
