"""The system prompt editor of the agent overlay: the parts are tabs over ONE big editor (platform revamp plan 05, Task 2; design section 9).

``AG_PromptTabs`` (``ui/components/agents.jsx``) replaces the column of small textareas: "Part 1", "Part 2" and "+ Part" above one editor of 12 rows that can
be dragged taller, "Remove part" (never the last part), and a Full screen toggle that lifts the box over the whole window. It runs here in V8 under a host
that keeps the parts and closes on Escape the way the overlay's Modal does, on the real Escape stack (``ui/foundation/escape-stack.js``).

Review focus 3: removing the selected part must not send the next keystrokes into a part that is no longer there. Review focus 4: Escape in full screen
leaves full screen first, so the draft in the overlay behind it is not lost.
"""

from __future__ import annotations

import pytest

from tests.ui._agent_form_harness import (
    agent_form,
    click,
    closes,
    mount_prompt_tabs,
    parts,
    press_escape,
    prop,
    set_parts,
    testids_under,
    text_of,
    type_into,
)


@pytest.fixture
def ctx():
    with agent_form() as c:
        yield c


def tab_ids(ctx):
    return [t for t in testids_under(ctx, "agent-system-prompt-tabs") if t.startswith("agent-system-prompt-tab-")]


def test_one_part_shows_one_tab_and_remove_part_is_disabled(ctx):
    mount_prompt_tabs(ctx, ["You triage refunds."])
    assert tab_ids(ctx) == ["agent-system-prompt-tab-0"]
    assert prop(ctx, "agent-system-prompt-remove", "disabled") is True
    assert prop(ctx, "agent-system-prompt-editor", "value") == "You triage refunds."
    assert prop(ctx, "agent-system-prompt-editor", "aria-label") == "System prompt part 1"


def test_plus_part_adds_an_empty_part_and_selects_it(ctx):
    mount_prompt_tabs(ctx, ["A"])
    click(ctx, "agent-system-prompt-add")
    assert parts(ctx) == ["A", ""]
    assert prop(ctx, "agent-system-prompt-tab-1", "aria-selected") is True
    assert prop(ctx, "agent-system-prompt-editor", "aria-label") == "System prompt part 2"
    assert prop(ctx, "agent-system-prompt-remove", "disabled") is False


def test_typing_changes_only_the_selected_part(ctx):
    mount_prompt_tabs(ctx, ["A", "B"])
    click(ctx, "agent-system-prompt-tab-1")
    type_into(ctx, "agent-system-prompt-editor", "B2")
    assert parts(ctx) == ["A", "B2"]
    click(ctx, "agent-system-prompt-tab-0")
    assert prop(ctx, "agent-system-prompt-editor", "value") == "A"


def test_removing_the_middle_part_selects_the_next_and_typing_goes_into_it(ctx):  # Review Focus 3
    mount_prompt_tabs(ctx, ["A", "B", "C"])
    click(ctx, "agent-system-prompt-tab-1")
    click(ctx, "agent-system-prompt-remove")
    assert parts(ctx) == ["A", "C"]
    assert prop(ctx, "agent-system-prompt-editor", "value") == "C"
    type_into(ctx, "agent-system-prompt-editor", "C2")
    assert parts(ctx) == ["A", "C2"]


def test_removing_the_last_part_selects_the_one_before_it(ctx):
    mount_prompt_tabs(ctx, ["A", "B"])
    click(ctx, "agent-system-prompt-tab-1")
    click(ctx, "agent-system-prompt-remove")
    assert parts(ctx) == ["A"]
    assert prop(ctx, "agent-system-prompt-tab-0", "aria-selected") is True


def test_parts_that_shrink_from_outside_keep_a_valid_selection(ctx):
    mount_prompt_tabs(ctx, ["A", "B", "C"])
    click(ctx, "agent-system-prompt-tab-2")
    set_parts(ctx, ["A"])
    assert prop(ctx, "agent-system-prompt-editor", "value") == "A"
    assert prop(ctx, "agent-system-prompt-editor", "aria-label") == "System prompt part 1"


def test_the_editor_is_twelve_rows_and_can_be_dragged_taller(ctx):
    mount_prompt_tabs(ctx, ["A"])
    assert prop(ctx, "agent-system-prompt-editor", "rows") == 12
    assert prop(ctx, "agent-system-prompt-editor", "style")["resize"] == "vertical"


def test_the_tabs_are_a_named_tablist(ctx):
    mount_prompt_tabs(ctx, ["A", "B"])
    assert prop(ctx, "agent-system-prompt-tabs", "role") == "tablist"
    assert prop(ctx, "agent-system-prompt-tabs", "aria-label") == "System prompt parts"
    assert prop(ctx, "agent-system-prompt-tab-1", "role") == "tab"
    assert text_of(ctx, "agent-system-prompt-tab-1") == "Part 2"
    assert text_of(ctx, "agent-system-prompt-add") == "+ Part"


def test_full_screen_toggles_and_escape_leaves_it_before_closing_the_overlay(ctx):  # Review Focus 4
    mount_prompt_tabs(ctx, ["A"])
    click(ctx, "agent-system-prompt-fullscreen")
    assert prop(ctx, "agent-system-prompt", "data-fullscreen") == "true"
    assert prop(ctx, "agent-system-prompt-fullscreen", "aria-pressed") is True
    assert text_of(ctx, "agent-system-prompt-fullscreen") == "Exit full screen"
    press_escape(ctx)
    assert prop(ctx, "agent-system-prompt", "data-fullscreen") == "false" and closes(ctx) == 0
    press_escape(ctx)
    assert closes(ctx) == 1
