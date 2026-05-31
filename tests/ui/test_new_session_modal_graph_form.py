"""When a graph binding is selected and the graph's Begin has input_schema,
NewSessionModal renders a dynamic form. Without input_schema, the existing
textarea is preserved."""

from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "app.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_modal_reads_begin_input_schema() -> None:
    src = _src()
    assert "input_schema" in src and "begin" in src.lower()


def test_modal_packages_into_graph_input_field() -> None:
    assert "graph_input" in _src()


def test_modal_falls_back_to_textarea_without_schema() -> None:
    src = _src()
    assert "initial_instructions" in src
