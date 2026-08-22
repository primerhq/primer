"""The Trace tab (spec sections 4 and 8).

It is the exhaustive record: raw arguments live HERE and nowhere else,
which is what lets the transcript chips stay plain language. It opens as
a tab in a second group, never as an overlay.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "components" / "shell" / "sh-trace-tab.jsx"
HOST = UI / "components" / "shell" / "sh-doc-host.jsx"
MANIFEST = UI / "fixtures" / "shell" / "manifest.json"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_trace_became_a_doc_kind_everywhere_at_once() -> None:
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert man["doc_kinds"] == ["session", "file", "diff", "wiki", "trace"]
    url = (UI / "foundation" / "shell-url.js").read_text(encoding="utf-8")
    assert '"trace"' in url
    assert "`trace`" in (UI / "fixtures" / "shell" / "README.md").read_text(
        encoding="utf-8"
    )


def test_it_re_hosts_the_s7_panel_rather_than_re_implementing_it() -> None:
    """Pinned decision 8: section 5's "reused (S7)" designation binds to
    window.SessionTracePanel, which S7 P4 Task 20 built props-only so this
    shell could re-host it unchanged."""
    src = _src()
    assert "window.SessionTracePanel" in src
    assert (
        ROOT / "ui" / "components" / "shared" / "session-trace.jsx"
    ).is_file(), "S7 Task 20 must have landed before this task runs"


def test_no_second_tree_renderer_lives_here() -> None:
    """The node tree, the raw-argument expander and the timeline fetch are
    the S7 panel's; duplicating them is what decision 8 now forbids."""
    src = _src()
    for banned in ("node.children", "JSON.stringify", "SH_api.timeline"):
        assert banned not in src, banned


def test_split_right_is_how_trace_opens() -> None:
    """Section 8: trace opens split-beside-transcript, never in an
    overlay."""
    src = HOST.read_text(encoding="utf-8")
    assert "trace.split" in src
    assert "Split Trace" in src
    assert "splitRight" in src
    assert "SH_traceRef" in src


def test_the_ref_round_trips() -> None:
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    body = _src()
    # Slice the two pure functions only: everything from SH_TraceTab on is
    # JSX, which MiniRacer cannot parse.
    start = body.index("function SH_traceRef")
    end = body.index("function SH_TraceTab")
    ctx.eval(body[start:end])
    assert ctx.eval('SH_traceRef("sess-1", 7)') == "sess-1:7"
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_parseTraceRef("sess-1:7"))'
    ))
    assert out == {"sid": "sess-1", "turnNo": 7}
    assert ctx.eval('SH_parseTraceRef("garbage") === null') is True
