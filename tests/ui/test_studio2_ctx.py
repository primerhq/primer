"""Static checks for the Studio2 workspace-context model (plan task 7)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_ctx_api() -> None:
    src = _read("components/studio2/s2-ctx.jsx")
    for needle in ["window.S2_Ctx", "pin(", "unpin(", "noteActiveDoc(",
                   '"ctx:unpin"', "Context: pin to",
                   "Context: follow active tab"]:
        assert needle in src


def test_ctx_only_follows_sessions_and_respects_pin() -> None:
    src = _read("components/studio2/s2-ctx.jsx")
    assert 'kind === "session"' in src
    assert "if (isPinned) return;" in src


def test_chip_shows_auto_or_pinned() -> None:
    src = _read("components/studio2/s2-shell.jsx")
    assert "s2-ctx-chip" in src
    assert "auto" in src and "pinned" in src and "S2_Ctx" in src


def test_shell_polls_workspaces_and_registers_pins() -> None:
    src = _read("components/studio2/s2-shell.jsx")
    assert "/workspaces?limit=200" in src
    assert "S2_registerCtxPins" in src


def test_files_group_routes_to_classic_studio() -> None:
    src = _read("components/studio2/s2-nav.jsx")
    assert '"legacy:/workspaces/" + ctxWs' in src
    assert "Open a session first" in src
