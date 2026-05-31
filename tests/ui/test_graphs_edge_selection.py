"""Edge selection state present in the graphs editor."""

from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "graphs.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_selected_edge_id_state_present() -> None:
    src = _src()
    assert "selectedEdgeId" in src
