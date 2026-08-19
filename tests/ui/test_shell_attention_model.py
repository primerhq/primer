"""Attention is computed from S1's envelopes plus the approvals endpoint.

Amendment m10: S6 is never consulted. Pinned decision 17: the tier is a
function of the yield's tool_name, because that is the only consequence
signal the approved backends carry.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "shell-attention.js"
FIXTURES = ROOT / "ui" / "fixtures" / "shell"


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    pending = (FIXTURES / "pending-yields.json").read_text(encoding="utf-8")
    records = (FIXTURES / "approval-records.json").read_text(encoding="utf-8")
    ctx.eval(f"var PENDING = {pending}; var RECORDS = {records};")
    return ctx


def test_module_is_pure_and_registered() -> None:
    src = MODULE.read_text(encoding="utf-8")
    for banned in ("document.", "apiFetch", "React"):
        assert banned not in src, banned
    assert 'src="foundation/shell-attention.js"' in (
        ROOT / "ui" / "index.html"
    ).read_text(encoding="utf-8")


def test_it_never_reaches_for_a_trigger_or_channel() -> None:
    """Amendment m10 in one assertion."""
    src = MODULE.read_text(encoding="utf-8").lower()
    for banned in ("trigger", "channel", "subscription"):
        assert banned not in src, banned


def test_gates_and_questions_are_interrupt_tier() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_toAttentionItems({pending: PENDING.items, '
        'records: RECORDS.items}).map(function (i) '
        '{ return [i.toolName, i.tier, i.resolved]; }))'
    ))
    by_name = {row[0]: row for row in out}
    assert by_name["ask_approval"][1] == "interrupt"
    assert by_name["ask_user"][1] == "interrupt"


def test_an_unknown_pending_yield_is_ambient_not_an_interrupt() -> None:
    """Interrupts are spent extremely sparingly."""
    ctx = _ctx()
    tier = ctx.eval(
        'SH_tierFor({toolName: "workspace__grep", resolved: false})'
    )
    assert tier == "ambient"


def test_resolved_records_are_digest_and_remain_queryable() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        'JSON.stringify(SH_toAttentionItems({pending: [], '
        'records: RECORDS.items}).map(function (i) '
        '{ return [i.tier, i.resolved]; }))'
    ))
    assert out and all(row == ["digest", True] for row in out)


def test_the_decision_preview_survives_into_the_item() -> None:
    """A decision card without the literal command or diff is a
    free-text question, which section 8 forbids."""
    ctx = _ctx()
    preview = ctx.eval(
        'SH_toAttentionItems({pending: PENDING.items, records: []})[0].preview'
    )
    assert "stripe-signature" in preview


def test_snooze_and_mute_filter_without_deleting() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var items = SH_toAttentionItems({pending: PENDING.items, records: []});
          var triage = SH_emptyTriage();
          triage.mutedSessions["sess-77de01aa"] = true;
          var kept = SH_applyTriage(items, triage);
          return JSON.stringify([items.length, kept.length]);
        })()
        """
    ))
    assert out == [2, 1]


def test_approved_by_map_is_keyed_on_the_tool_call_id() -> None:
    """The "on behalf of" stamp needs to know which call a human let
    through."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        "JSON.stringify(SH_approvedByMap(RECORDS.items))"
    ))
    assert "tc-77" in out
    assert "tc-76" not in out, "a rejected call was never executed"


def test_the_triage_key_is_per_account() -> None:
    ctx = _ctx()
    assert ctx.eval('SH_triageKey("usman")') == "primer.shell.triage:usman"
