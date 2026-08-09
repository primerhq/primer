"""Static checks for the Studio2 right rail (plan task 11)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_attention_and_activity_sections() -> None:
    src = _read("components/studio2/s2-right.jsx")
    assert "attention" in src and "activity" in src


def test_reuses_classic_components_not_a_fork() -> None:
    src = _read("components/studio2/s2-right.jsx")
    assert "window.AttentionBar" in src
    assert "window.WorkspaceTap" in src


def test_scoped_to_ctx_with_honest_empty_state() -> None:
    src = _read("components/studio2/s2-right.jsx")
    assert "S2_Ctx.useCtx" in src
    assert "global endpoint" in src  # the deferral is stated, not hidden


def test_shell_mounts_right() -> None:
    assert "S2_Right" in _read("components/studio2/s2-shell.jsx")
