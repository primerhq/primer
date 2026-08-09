"""Static checks for the Studio2 document registry + tabs (plan task 3)."""

from pathlib import Path

UI = Path(__file__).resolve().parents[2] / "ui"


def _read(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def test_docs_api_surface() -> None:
    src = _read("components/studio2/s2-docs.jsx")
    for needle in ["registerKind", "open(", "close(", "activate(",
                   "window.S2_Docs", "studio2:tabs", "?open="]:
        assert needle in src


def test_deep_link_wins_over_stored_active() -> None:
    src = _read("components/studio2/s2-docs.jsx")
    assert "restore(" in src
    assert "open=" in src and "match(" in src


def test_dirty_close_confirms() -> None:
    assert "confirm(" in _read("components/studio2/s2-docs.jsx")


def test_tabbar_renders_dirty_dot_and_close() -> None:
    src = _read("components/studio2/s2-tabbar.jsx")
    assert "dirty" in src and "×" in src
    assert "aria-selected" in src


def test_empty_state_teaches_the_keyboard() -> None:
    # Design-pack rule 14: composed empty states that say how to
    # populate - here, the keyboard spine.
    src = _read("components/studio2/s2-tabbar.jsx")
    assert "s2-kbd" in src


def test_shell_mounts_tabbar_and_active_doc_and_restores() -> None:
    src = _read("components/studio2/s2-shell.jsx")
    assert "S2_TabBar" in src and "S2_ActiveDoc" in src
    assert "S2_Docs.restore()" in src


def test_shortcuts_registered() -> None:
    src = _read("components/studio2/s2-docs.jsx")
    assert '"tab:close"' in src and '"tab:" + n' in src
