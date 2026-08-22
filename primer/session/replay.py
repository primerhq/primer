"""Read-time reconstruction of what a session log currently shows.

The log is append-only: compaction and rewind never delete a line, they
append a marker describing what a reader should now see. That keeps the
event history intact for audit while letting the conversation be folded
or cut back.

Visibility is therefore computed, not stored. Walk the log in append
order maintaining a visible set:

* a ``compaction_marker`` folds everything CURRENTLY visible before it
  into itself, so the marker stands in for the span it summarises;
* a ``rewind_marker`` drops every currently visible row whose seq is
  greater than its ``to_seq``.

Both rules act on the visible set rather than on raw file order, which
is what makes them compose. Rewind, continue, rewind again nests
correctly, and a compaction that lived inside a rewound span stays
hidden instead of resurfacing as an orphan summary.
"""

from __future__ import annotations

import json
from typing import Any

from primer.model.workspace_session import SessionMessageKind

_COMPACTION = SessionMessageKind.COMPACTION_MARKER.value
_REWIND = SessionMessageKind.REWIND_MARKER.value


def _parse(raw_lines: list[str]) -> list[dict[str, Any]]:
    """Record lines only: the log interleaves plain role/parts messages."""
    out: list[dict[str, Any]] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # a crash can leave a half-written line
        if isinstance(obj, dict) and "kind" in obj and "seq" in obj:
            out.append(obj)
    return out


def visible_records(raw_lines: list[str]) -> list[dict[str, Any]]:
    """Return the records a reader should currently see, in seq order.

    Structural markers are consumed by the walk and never returned:
    a rewind marker is an instruction, not content. A compaction marker
    IS returned, because it carries the summary standing in for the
    span it folded.
    """
    visible: list[dict[str, Any]] = []
    for rec in _parse(raw_lines):
        kind = rec.get("kind")
        if kind == _REWIND:
            to_seq = (rec.get("payload") or {}).get("to_seq")
            if isinstance(to_seq, int):
                visible = [r for r in visible if r["seq"] <= to_seq]
            continue
        if kind == _COMPACTION:
            # The marker replaces everything visible before it.
            visible = [rec]
            continue
        visible.append(rec)
    return visible


__all__ = ["visible_records"]
