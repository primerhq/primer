"""Approval-policy card + decisions-audit row derivation logic (Phase-2c:
platform-approvals-staged / rev-2 synthesis Wave 3 item 49).

Runs the real JS in MiniRacer (same technique as tests/ui/test_audio_
resample.py) rather than grepping for source text, so branch logic in
the status/decided-by derivation is actually exercised. The helpers
(NV_approvalToolPattern..NV_approvalAt) are pure functions with no JSX
and no window.* dependency, so they're sliced out and evaluated in
isolation instead of transpiling the whole (JSX) file.

Backend ground truth these pin against:
* primer/model/tool_approval.py - ToolApprovalRecord/ToolApprovalPolicy
  real field names (toolset_id, tool_name, decided_by, reason, ...).
* primer/worker/yield_runtime.py's classify_approval_payload - a
  session-bound timeout resolves to (decision="rejected",
  reason="timed-out"); a cancel resolves to (decision="rejected",
  reason=<given> or "cancelled"); decided_by is None for both
  (ToolApprovalRecord's own field doc: "None for synthesized verdicts").
"""

from __future__ import annotations

from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"
NV_PLATFORM = UI / "components" / "console" / "nv-platform.jsx"


def _helpers_src() -> str:
    src = NV_PLATFORM.read_text(encoding="utf-8")
    start = src.index("function NV_approvalToolPattern(row)")
    end = src.index("var NV_PLAT_PAGE_SIZE")
    return src[start:end]


def _ctx():
    py_mini_racer = pytest.importorskip("py_mini_racer")
    ctx = py_mini_racer.MiniRacer()
    ctx.eval(_helpers_src())
    return ctx


def _call(ctx, fn: str, *args) -> object:
    import json

    args_js = ", ".join(json.dumps(a) for a in args)
    return ctx.eval(f"JSON.stringify({fn}({args_js}))")


def test_tool_pattern_is_the_mono_compound_id() -> None:
    ctx = _ctx()
    row = {"toolset_id": "_workspaces", "tool_name": "write_file"}
    assert _call(ctx, "NV_approvalToolPattern", row) == '"_workspaces__write_file"'


def test_explicit_human_reject_is_plain_rejected() -> None:
    ctx = _ctx()
    r = {"decision": "rejected", "decided_by": "usman", "reason": "bad idea"}
    assert _call(ctx, "NV_approvalDerivedStatus", r) == '"rejected"'


def test_session_bound_timeout_derives_to_timeout() -> None:
    # decided_by is None (synthesized) and reason is the exact literal
    # classify_approval_payload emits for a YieldTimeout.
    ctx = _ctx()
    r = {"decision": "rejected", "decided_by": None, "reason": "timed-out"}
    assert _call(ctx, "NV_approvalDerivedStatus", r) == '"timeout"'


def test_session_bound_cancel_derives_to_cancelled() -> None:
    ctx = _ctx()
    r = {"decision": "rejected", "decided_by": None, "reason": "cancelled"}
    assert _call(ctx, "NV_approvalDerivedStatus", r) == '"cancelled"'


def test_literal_timeout_and_cancelled_decisions_pass_through() -> None:
    # Reachable via primer/session/abandon.py, not the session-bound
    # gate path - already-literal values are honoured as-is.
    ctx = _ctx()
    assert _call(ctx, "NV_approvalDerivedStatus", {"decision": "timeout"}) == '"timeout"'
    assert _call(ctx, "NV_approvalDerivedStatus", {"decision": "cancelled"}) == '"cancelled"'


def test_approved_and_pending_pass_through_unchanged() -> None:
    ctx = _ctx()
    assert _call(ctx, "NV_approvalDerivedStatus", {"decision": "approved"}) == '"approved"'
    assert _call(ctx, "NV_approvalDerivedStatus", {"decision": "pending"}) == '"pending"'


def test_status_color_groups_timeout_and_cancelled_with_rejected() -> None:
    ctx = _ctx()
    for status in ("rejected", "timeout", "cancelled"):
        assert _call(ctx, "NV_approvalStatusColor", status) == '"var(--red)"'
    assert _call(ctx, "NV_approvalStatusColor", "approved") == '"var(--green)"'
    assert _call(ctx, "NV_approvalStatusColor", "pending") == '"var(--text-3)"'


def test_decided_by_quotes_the_reason_for_an_explicit_reject() -> None:
    ctx = _ctx()
    r = {"decision": "rejected", "decided_by": "usman", "reason": "use the test runner"}
    assert _call(ctx, "NV_approvalDecidedBy", r, "rejected") == (
        '"usman — \\"use the test runner\\""'
    )


def test_decided_by_has_no_actor_for_a_plain_approve() -> None:
    ctx = _ctx()
    r = {"decision": "approved", "decided_by": "usman", "reason": None}
    assert _call(ctx, "NV_approvalDecidedBy", r, "approved") == '"usman"'


def test_timeout_decided_by_derives_elapsed_minutes_from_the_record_itself() -> None:
    # NOT the policy's configured timeout_seconds - the record's own
    # requested_at/decided_at span is what actually elapsed, which is
    # correct even when the GLOBAL yield cap fired instead of a
    # per-policy timeout (the staging gotcha that surfaced this).
    ctx = _ctx()
    r = {
        "decision": "rejected", "decided_by": None, "reason": "timed-out",
        "requested_at": "2026-08-15T17:20:00Z",
        "decided_at": "2026-08-15T17:50:00Z",
    }
    assert _call(ctx, "NV_approvalDecidedBy", r, "timeout") == '"30m elapsed → rejected"'


def test_cancelled_decided_by_quotes_a_real_cancel_reason() -> None:
    ctx = _ctx()
    r = {"decision": "rejected", "decided_by": None, "reason": "operator abandoned chat"}
    assert _call(ctx, "NV_approvalDecidedBy", r, "cancelled") == (
        '"cancelled — \\"operator abandoned chat\\""'
    )


def test_cancelled_decided_by_has_no_quote_for_the_canned_default() -> None:
    ctx = _ctx()
    r = {"decision": "rejected", "decided_by": None, "reason": "cancelled"}
    assert _call(ctx, "NV_approvalDecidedBy", r, "cancelled") == '"cancelled"'


def test_at_renders_bare_time_for_same_day_and_dated_otherwise() -> None:
    ctx = _ctx()
    # Same-day: freeze "now" to a fixed instant on the same calendar day
    # as the record so this doesn't depend on when the suite runs.
    now_iso = "2026-08-15T23:00:00"
    ctx.eval(f'var __RealDate = Date; Date = function(...a) {{ return a.length ? new __RealDate(...a) : new __RealDate("{now_iso}"); }};')
    assert _call(ctx, "NV_approvalAt", "2026-08-15T17:50:00") == '"17:50"'
    assert _call(ctx, "NV_approvalAt", "2026-08-14T17:50:00") == '"Aug 14 · 17:50"'
