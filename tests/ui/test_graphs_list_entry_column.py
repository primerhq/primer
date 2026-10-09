"""graphs.jsx: the list's entry is the Begin node's id, not the model's long-gone entry_node_id.

The model replaced ``entry_node_id`` with the topology rule 'exactly one Begin node' (docs/dev/subsystems/graphs.md), so the list's mobile meta line never drew and the desktop Entry column always drew the muted dash. The row already carries ``g.nodes`` (the node count reads it), so the card meta and the table column name the Begin node from it instead.
"""
from __future__ import annotations
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "graphs.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_graphs_jsx_no_longer_reads_the_dead_entry_node_id() -> None:
    assert "entry_node_id" not in _src()


def test_the_list_derives_the_entry_from_the_begin_node() -> None:
    assert 'n.kind === "begin"' in _src()


def test_the_table_keeps_its_entry_column() -> None:
    assert "<th>Entry</th>" in _src()
