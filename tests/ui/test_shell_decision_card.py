"""Structured decision cards (spec section 8).

Proposed action with a literal command or diff preview, then approve /
reject-with-feedback. v1 has no edit-then-approve (amendment C6). Never a
blocking modal: the user must be able to keep scrolling while judging.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-decision-card.jsx"
SESSION = UI / "components" / "shell" / "sh-session-doc.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_registered_in_the_bundle() -> None:
    assert 'src="components/shell/sh-decision-card.jsx"' in (
        UI / "index.html"
    ).read_text(encoding="utf-8")
    assert "window.SH_DecisionCard = SH_DecisionCard;" in _src()


def test_it_is_never_a_blocking_modal() -> None:
    src = _src()
    assert "aria-modal" not in src or 'aria-modal="false"' in src
    for banned in ("<dialog", "showModal", "inert"):
        assert banned not in src, banned


def test_the_literal_action_is_shown_before_the_buttons() -> None:
    src = _src()
    assert 'data-testid="shell-decision-preview"' in src
    assert src.index("shell-decision-preview") < src.index(
        "shell-decision-approve"
    )


def test_approve_and_reject_with_feedback_both_exist() -> None:
    src = _src()
    assert 'data-testid="shell-decision-approve"' in src
    assert 'data-testid="shell-decision-reject"' in src
    assert 'data-testid="shell-decision-reason"' in src
    assert "SH_api.approve" in src and "SH_api.reject" in src


def test_edit_then_approve_is_absent_in_v1() -> None:
    """Amendment C6 sends it to the programme follow-ups."""
    src = _src().lower()
    assert "edit-then-approve" not in src
    assert "amend" not in src


def test_a_question_gets_an_answer_field_not_an_approve_button() -> None:
    src = _src()
    assert 'data-testid="shell-decision-answer"' in src
    assert "SH_api.answer" in src


def test_the_same_card_renders_inline_in_the_transcript() -> None:
    """Rendered twice from one source (section 8)."""
    src = SESSION.read_text(encoding="utf-8")
    assert "SH_DecisionCard" in src
    assert "sessionPendingYields" in src
