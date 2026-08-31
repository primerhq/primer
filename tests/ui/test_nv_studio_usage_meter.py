"""UX reconcile wave 1 (audit A item 13): the session header's context
meter used-of-total label.

Pure function (no React/JSX) - extracted and executed via MiniRacer,
same style as test_console_system.py's own NV_sysNavsFor test - a
substring grep would not catch a label FORMAT regression the way an
actual call does.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STUDIO = (
    ROOT / "ui" / "components" / "console" / "nv-studio.jsx"
).read_text(encoding="utf-8")


def _usage_of(session_js):
    from py_mini_racer import MiniRacer

    start = STUDIO.index("function NV_usageOf(")
    end = STUDIO.index("\n}\n", start) + 2
    fn_src = STUDIO[start:end]
    ctx = MiniRacer()
    ctx.eval(fn_src)
    ctx.eval(f"var out = NV_usageOf({session_js});")
    return json.loads(ctx.eval("JSON.stringify(out)"))


def test_label_shows_used_over_total() -> None:
    out = _usage_of(
        '{usage: {total_input_tokens: 30000, total_output_tokens: 8000}, '
        'context_length: 200000}'
    )
    assert out["label"] == "38k / 200k", out
    assert out["pct"] == 19, out


def test_label_falls_back_to_used_only_without_a_context_length() -> None:
    out = _usage_of(
        '{usage: {total_input_tokens: 12000, total_output_tokens: 0}, '
        'context_length: 0}'
    )
    assert out["label"] == "12k", out
    assert out["pct"] == 0, out


def test_no_usage_yet_is_an_empty_meter_not_a_lie() -> None:
    out = _usage_of("{}")
    assert out == {"pct": 0, "label": ""}
