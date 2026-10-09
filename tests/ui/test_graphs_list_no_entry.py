"""graphs.jsx: the graphs list no longer reads entry_node_id, and the table's column count stays consistent.

The model replaced ``entry_node_id`` with the topology rule 'exactly one Begin node' (docs/dev/subsystems/graphs.md), so the list's mobile meta line never drew and the desktop Entry column always drew the muted dash. Both dead reads are gone, the Entry column (its th and td) is dropped, and every empty-state colSpan is lowered by one so the header count equals the cell count.
"""
from __future__ import annotations
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "ui" / "components" / "graphs.jsx"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _table() -> str:
    """The GraphsPage desktop table, from its opening tag to the closing tag (the page draws exactly one table)."""
    src = _src()
    start = src.index('<table className="tbl">')
    return src[start : src.index("</table>", start) + len("</table>")]


def test_graphs_jsx_does_not_read_entry_node_id() -> None:
    assert "entry_node_id" not in _src()


def test_the_table_header_count_equals_every_colspan() -> None:
    table = _table()
    th_count = len(re.findall(r"<th[\s>]", table))
    spans = [int(value) for value in re.findall(r"colSpan=\{(\d+)\}", table)]
    assert spans, "the empty-state rows should span the full table width"
    assert all(span == th_count for span in spans), (
        f"colSpan values {sorted(set(spans))} do not equal the {th_count} <th> cells"
    )
