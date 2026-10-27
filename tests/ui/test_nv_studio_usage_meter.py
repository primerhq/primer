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


# G9-3 (audit): the meter exists to warn "near overflow". Its numerator is the
# latest model call's actual prompt size (last_input_tokens), not the
# session-lifetime sum: the cumulative numerator pins at 100% after a few
# moderate turns and loses the overflow signal the meter exists for.


def test_the_meter_reads_the_current_prompt_not_the_lifetime_sum() -> None:
    # A 4-turn session: the cumulative totals say 90k, the live prompt is 25k on a 32k window.
    out = _usage_of(
        '{context_length: 32000, usage: {turns: 4, last_input_tokens: 25000, '
        'last_output_tokens: 5000, total_input_tokens: 70000, total_output_tokens: 20000}}'
    )
    assert out["label"] == "25k / 32k", out
    assert out["pct"] == 78, out


def test_the_fallback_keeps_the_cumulative_sum_when_no_call_has_reported() -> None:
    # Over-correction guard (green pre-fix and post-fix): when no call has reported usage
    # yet (last_input_tokens 0 or absent) and the totals are above zero, the meter keeps
    # the cumulative reading.
    out = _usage_of(
        '{context_length: 200000, usage: {last_input_tokens: 0, '
        'total_input_tokens: 30000, total_output_tokens: 8000}}'
    )
    assert out["label"] == "38k / 200k", out
    assert out["pct"] == 19, out
    out = _usage_of(
        '{context_length: 200000, usage: {total_input_tokens: 30000, total_output_tokens: 8000}}'
    )
    assert out["label"] == "38k / 200k", out
    assert out["pct"] == 19, out


def test_the_fallback_label_stays_used_only_without_a_context_length() -> None:
    # Same guard for the label shape: no context length on the cumulative path is the used-only label.
    out = _usage_of(
        '{context_length: 0, usage: {last_input_tokens: 0, '
        'total_input_tokens: 12000, total_output_tokens: 5000}}'
    )
    assert out["label"] == "17k", out
    assert out["pct"] == 0, out
