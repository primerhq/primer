"""Platform wave P1b item 4 + addendum item 8: agents.jsx create/edit
modal - verb chip, Tools footnote + counter chip, the disabled
Autonomous segment (Agent has no such field), the Voice pairing note,
and the system_prompt multi-part editor that superseded the original
brief's "leave the textarea alone" instruction.

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


# ---- verb chip --------------------------------------------------------


def test_modal_has_a_verb_chip() -> None:
    modal = _modal_src()
    assert 'data-testid="agent-modal-verb-chip"' in modal
    assert 'verb: {isEdit ? "Edit" : "Create"} Agent' in modal


# ---- Tools tab: footnote + already-live y/w/r/n badges -----------------


def test_tools_footnote_is_verbatim() -> None:
    modal = _modal_src()
    assert 'data-testid="agent-tools-footnote"' in modal
    assert "y yields · w" in modal
    assert "workspace · r role · n notifying" in modal


def test_capability_badges_are_already_live_not_a_p2_placeholder() -> None:
    """The brief assumed y/w/r/n chips were P2-gated; CapabilityBadges
    (ui/components/shared/capability-badges.jsx) already renders real
    flags on every tool row from data GET /tools already serves. Pin
    that the working component stayed wired rather than being ripped
    out for an empty P2 slot."""
    modal = _modal_src()
    assert "CapabilityBadges" in modal


# ---- Autonomous: disabled, honest no-op ---------------------------------


def test_autonomous_segment_is_disabled() -> None:
    modal = _modal_src()
    assert 'data-testid="agent-autonomous-segment"' in modal
    assert 'aria-disabled="true"' in modal
    assert "pointerEvents: \"none\"" in modal


def test_autonomous_note_explains_the_gap() -> None:
    modal = _modal_src()
    assert 'data-testid="agent-autonomous-note"' in modal
    assert "per-session control" in modal


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


def test_system_prompt_editor_is_a_repeatable_list_not_one_textarea() -> None:
    modal = _modal_src()
    assert "systemPromptParts.map((part, i) =>" in modal
    assert 'data-testid={`agent-system-prompt-part-${i}`}' in modal


def test_system_prompt_add_part_appends_an_empty_string() -> None:
    modal = _modal_src()
    assert 'data-testid="agent-system-prompt-add"' in modal
    assert "setSystemPromptParts(systemPromptParts.concat([\"\"]))" in modal


def test_system_prompt_remove_part_is_disabled_at_one_remaining_part() -> None:
    """Can't remove the last part - the editor always has at least one
    row, mirroring the legacy-load default."""
    modal = _modal_src()
    assert 'data-testid={`agent-system-prompt-remove-${i}`}' in modal
    assert "disabled={systemPromptParts.length === 1}" in modal


def test_system_prompt_submit_drops_empty_parts_and_sends_an_array() -> None:
    """No delimiter tricks: each part is one array element, and a
    part left blank after an add-then-leave-blank is dropped rather
    than sent as a hole in the array."""
    modal = _modal_src()
    assert (
        "system_prompt: systemPromptParts.map((p) => p.trim()).filter(Boolean),"
        in modal
    )


def test_bundle_transpiles_with_agents_p1b() -> None:
    from primer.api._jsx_bundle import build_jsx_bundle

    build_jsx_bundle.cache_clear()
    etag, body = build_jsx_bundle(UI)
    assert etag and body, "bundle did not build (Babel/vendor missing?)"
    text = body.decode("utf-8")
    assert "/* === components/agents.jsx === */" in text
