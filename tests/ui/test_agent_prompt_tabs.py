"""The system prompt editor of the agent overlay: the parts are tabs over ONE big editor (platform revamp plan 05, Task 2; design section 9).

``AG_PromptTabs`` (``ui/components/agents.jsx``) replaces the column of small textareas: "Part 1", "Part 2" and "+ Part" above one editor of 12 rows that can
be dragged taller, "Remove part" (never the last part), and a Full screen toggle that lifts the box over the whole window. It runs here in V8 under a host
that keeps the parts and closes on Escape the way the overlay's Modal does, on the real Escape stack (``ui/foundation/escape-stack.js``).

Review focus 3: removing the selected part must not send the next keystrokes into a part that is no longer there. Review focus 4: Escape in full screen
leaves full screen first, so the draft in the overlay behind it is not lost. Full screen is also a focus trap (review of #779, required fix 2): Tab must not
walk from the full-screen editor on to the controls it covers, where the Enter on the covered Cancel closed the overlay and lost the draft. The trap itself
needs a DOM, so here the harness records the call to ``useFocusTrap`` and these tests pin its arguments; the Tab keys are pressed in a real browser by
``tests/ui_e2e/test_agent_overlay_journey.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

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
    trap_report,
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


def test_full_screen_is_above_every_layer_inside_the_dialog(ctx):
    """Found in a real browser: the tool picker's toolset headers are ``position: sticky`` at z-index 1, and a full-screen editor at z-index 1 sat UNDER them
    (a "system, 6 tools" row showed over the editor). The layers inside the dialog are those headers (read from the picker's source) and the kit's selector
    menu (z-index 3 in ``styles.css``); the full-screen box must be above all of them."""
    ui = Path(__file__).resolve().parents[2] / "ui"
    picker = [int(n) for n in re.findall(r"zIndex: (\d+)", (ui / "components" / "shared" / "tool-picker.jsx").read_text(encoding="utf-8"))]
    assert picker, "the picker's sticky header z-index is no longer in its source: update this test"
    mount_prompt_tabs(ctx, ["A"])
    click(ctx, "agent-system-prompt-fullscreen")
    style = prop(ctx, "agent-system-prompt", "style")
    assert style["position"] == "fixed" and style["zIndex"] > max(picker + [3])


# ---- full screen is a focus trap (review of #779, required fix 2) ------------------------------------------------------------------------------------------


def test_full_screen_is_a_focus_trap_on_its_own_box_that_opens_on_the_editor(ctx):
    """``useFocusTrap(boxRef, fullScreen, { initial: () => editorRef.current })``: off while the box sits in the form, on while it covers the window, attached to
    the box (the ref it is given is the box's own, so its Tab handler sees the box's keys) and opening on the editor."""
    mount_prompt_tabs(ctx, ["A"])
    idle = trap_report(ctx)
    assert idle["active"] is False and idle["onBox"] is True and idle["initialIsEditor"] is True
    click(ctx, "agent-system-prompt-fullscreen")
    assert prop(ctx, "agent-system-prompt", "data-fullscreen") == "true"
    on = trap_report(ctx)
    assert on["active"] is True and on["onBox"] is True and on["initialIsEditor"] is True
    assert on["calls"] > idle["calls"]


def test_the_trap_goes_off_when_full_screen_is_left_by_the_toggle_or_by_escape(ctx):
    mount_prompt_tabs(ctx, ["A"])
    click(ctx, "agent-system-prompt-fullscreen")
    assert trap_report(ctx)["active"] is True
    click(ctx, "agent-system-prompt-fullscreen")
    assert prop(ctx, "agent-system-prompt", "data-fullscreen") == "false" and trap_report(ctx)["active"] is False
    click(ctx, "agent-system-prompt-fullscreen")
    assert trap_report(ctx)["active"] is True
    press_escape(ctx)
    assert prop(ctx, "agent-system-prompt", "data-fullscreen") == "false" and trap_report(ctx)["active"] is False


def test_the_trap_follows_full_screen_on_every_render_even_after_the_parts_change(ctx):
    """The hook is called on EVERY render with the current state, so a part added or removed in full screen does not drop the trap."""
    mount_prompt_tabs(ctx, ["A", "B"])
    click(ctx, "agent-system-prompt-fullscreen")
    click(ctx, "agent-system-prompt-add")
    click(ctx, "agent-system-prompt-tab-0")
    click(ctx, "agent-system-prompt-remove")
    assert prop(ctx, "agent-system-prompt", "data-fullscreen") == "true"
    report = trap_report(ctx)
    assert report["active"] is True and report["onBox"] is True and report["initialIsEditor"] is True
