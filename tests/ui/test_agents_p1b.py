"""Platform wave P1b item 4 + addendum item 8: agents.jsx create/edit
modal - verb chip, Tools footnote + counter chip, the Voice pairing
note, and the system_prompt multi-part editor that superseded the
original brief's "leave the textarea alone" instruction.

Platform revamp plan 05 (design section 9) changed two of those: the
Autonomous segment is GONE (it is per session, WorkspaceSession.
autonomous; the Agent has no such field, so it was a disabled no-op), and
the system prompt parts are tabs over ONE editor (AG_PromptTabs) instead
of a column of textareas. The titles, the order of the fields and the
rest of the layout are pinned in tests/ui/test_agent_overlay_layout.py,
and what a save sends in tests/ui/test_agent_form_body.py.

Static-source checks only (the tests/ui suite convention). Sister of
tests/ui/test_agents_tools_selected_filter.py (counter chip ordering,
already retargeted) - this file covers the rest of the new P1b
surface that file doesn't.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "agents.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _modal_src() -> str:
    src = _src()
    start = src.index("function AG_NewAgentModal(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def _prompt_tabs_src() -> str:
    src = _src()
    start = src.index("function AG_PromptTabs(")
    return src[start:src.index("\n}\n", start) + 3]


# ---- verb chip --------------------------------------------------------


def test_modal_has_no_verb_chip() -> None:
    """The palette verb was a designer's annotation that shipped as UI (lead sweep L3). The title is plain now ("New agent", "Edit agent", "Managed agent";
    tests/ui/test_agent_overlay_layout.py), and the locked Id field carries the agent's id."""
    modal = _modal_src()
    assert "verb-chip" not in modal and "verb:" not in modal


# ---- Tools: footnote + already-live y/w/r/n badges ----------------------
#
# RETARGET (uiv2 Wave 2): both moved from AG_NewAgentModal's own inline
# Tools tab into the shared, reusable ToolPicker (ui/components/shared/
# tool-picker.jsx) - the modal now just mounts <window.ToolPicker>, it no
# longer renders these strings itself.


def _tool_picker_src() -> str:
    return (UI / "components" / "shared" / "tool-picker.jsx").read_text(encoding="utf-8")


def test_tools_footnote_is_verbatim() -> None:
    src = _tool_picker_src()
    assert 'data-testid="tool-picker-footnote"' in src
    assert "y yields · w" in src
    assert "workspace · r role · n notifying" in src
    # And the modal actually mounts the shared picker, not a local copy.
    assert "<window.ToolPicker " in _modal_src()


def test_capability_badges_are_already_live_not_a_p2_placeholder() -> None:
    """The brief assumed y/w/r/n chips were P2-gated; CapabilityBadges
    (ui/components/shared/capability-badges.jsx) already renders real
    flags on every tool row from data GET /tools already serves. Pin
    that the working component stayed wired rather than being ripped
    out for an empty P2 slot."""
    assert "CapabilityBadges" in _tool_picker_src()


# ---- Autonomous: gone (design 9) ----------------------------------------


def test_the_autonomous_segment_is_gone() -> None:
    """Autonomous is a per-session setting (WorkspaceSession.autonomous); the Agent has no such field, so the form's segment was a disabled no-op. Platform
    revamp design 9 removes it, with its note."""
    assert "agent-autonomous" not in _src()
    assert '["inherit", "on", "off"]' not in _src()


def test_autonomous_is_never_sent_in_the_submit_body() -> None:
    """No fake write: the submit body must not include an `autonomous`
    key anywhere, since Agent has no such field server-side."""
    modal = _modal_src()
    body_start = modal.index("const body = {")
    body_end = modal.index("};", body_start)
    body_src = modal[body_start:body_end]
    assert "autonomous" not in body_src


# ---- Voice pairing note --------------------------------------------------


def test_voice_pairing_note_present_when_a_voice_is_set() -> None:
    modal = _modal_src()
    assert 'data-testid="agent-voice-pairing-note"' in modal
    assert "pairs with the identity chip" in modal


# ---- Addendum item 8: system_prompt multi-part editor --------------------


def test_system_prompt_state_seeds_from_the_existing_array() -> None:
    modal = _modal_src()
    assert "_initialSystemPromptParts" in modal
    assert "Array.isArray(p) && p.length ? p : [\"\"]" in modal


def test_legacy_or_absent_system_prompt_renders_one_empty_part() -> None:
    """A legacy single-string load or a brand-new agent must still show
    at least one textarea, not zero."""
    modal = _modal_src()
    fn_start = modal.index("const _initialSystemPromptParts = () => {")
    fn_end = modal.index("};", fn_start)
    fn_src = modal[fn_start:fn_end]
    assert '[""]' in fn_src


def test_system_prompt_parts_are_tabs_over_one_editor() -> None:
    modal = _modal_src()
    assert "<AG_PromptTabs parts={systemPromptParts} onChange={setSystemPromptParts} />" in modal
    assert "systemPromptParts.map((part, i) =>" not in modal


def test_system_prompt_add_part_appends_an_empty_string() -> None:
    tabs = _prompt_tabs_src()
    assert 'data-testid="agent-system-prompt-add"' in tabs
    assert 'parts.concat([""])' in tabs


def test_system_prompt_remove_part_is_disabled_at_one_remaining_part() -> None:
    """Can't remove the last part - the editor always has at least one
    part, mirroring the legacy-load default."""
    tabs = _prompt_tabs_src()
    assert 'data-testid="agent-system-prompt-remove"' in tabs
    assert "disabled={parts.length === 1}" in tabs


def test_system_prompt_submit_sends_an_array_through_the_one_save_rule() -> None:
    """No delimiter tricks: each part is one array element. A part nobody touched goes back as stored, a typed one is trimmed, and a part left blank
    after an add-then-leave-blank is dropped rather than sent as a hole in the array (AG_systemPromptForSave; tests/ui/test_agent_form_body.py runs it)."""
    modal = _modal_src()
    assert "system_prompt: AG_systemPromptForSave(systemPromptParts, existing?.system_prompt)," in modal
    assert "systemPromptParts.map((p) => p.trim()).filter(Boolean)" not in modal


def test_bundle_transpiles_with_agents_p1b() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/agents.jsx === */" in text
