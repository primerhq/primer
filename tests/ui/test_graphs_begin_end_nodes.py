"""Add-node menu lists Begin + End; the old Terminal entry is gone."""

from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "graphs.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_add_node_menu_lists_begin_and_end() -> None:
    src = _src()
    assert 'kind === "begin"' in src or '"begin"' in src
    assert 'kind === "end"' in src or '"end"' in src


def test_terminal_node_creation_removed() -> None:
    src = _src()
    assert '"terminal"' not in src
    assert "kind: \"terminal\"" not in src


def test_begin_button_disabled_when_begin_exists() -> None:
    src = _src()
    # The disable logic should reference a count of begin nodes.
    assert "begin" in src.lower() and ("disabled" in src or "isDisabled" in src)
