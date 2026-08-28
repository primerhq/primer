"""The Studio center (wiring plan P2 T7): doc host tabs, the session
doc's inherited data layer + prototype render, cards, the trace SPLIT
(never an overlay), the composer discipline.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "ui" / "components" / "console"
HOST = (CONSOLE / "nv-doc-host.jsx").read_text(encoding="utf-8")
DOC = (CONSOLE / "nv-session-doc.jsx").read_text(encoding="utf-8")
FDOCS = (CONSOLE / "nv-file-docs.jsx").read_text(encoding="utf-8")


def test_tabs_have_vscode_semantics():
    assert 'data-preview=' in HOST
    assert "onDoubleClick" in HOST and "promoteDoc" in HOST
    assert "nv-tab-close" in HOST


def test_center_empty_is_a_prompt_with_actions():
    m = re.search(r'data-testid="nv-center-empty"[\s\S]{0,900}', HOST)
    assert m and "session.create" in m.group(0)


def test_session_doc_reuses_the_pure_modules():
    for mod in ("SA_toTranscript", "SH_nestSubagentRows",
                "SH_collapseTurns", "SH_toolChipLabel",
                "SH_statusFromTap", "SH_scrollDecision"):
        assert mod in DOC, mod


def test_composer_never_locks_and_dictation_never_sends():
    assert "queues mid-run" in DOC
    # rec\.onstop = function specifically (not just the word "onstop") -
    # the R3 review's mic-unmount-cleanup fix added a prose comment that
    # also happens to say "onstop", which an unscoped search landed on
    # instead of the real handler.
    m = re.search(r"rec\.onstop = function[\s\S]{0,700}", DOC)
    assert m and "setVal" in m.group(0) and "send()" not in m.group(0), (
        "dictation lands as editable text, never auto-sends"
    )


def test_mic_double_tap_latches_hold_to_talk_stays():
    """US-008 R3 item 5: hold-to-talk is unchanged (a normal release
    still stops via micStop); a confirmed double-tap latches recording
    on instead, and a click while latched stops it."""
    assert "MIC_DOUBLE_TAP_MS" in DOC
    assert 'onMouseDown={micDown} onMouseUp={micUp}' in DOC
    assert 'onMouseLeave={micLeave}' in DOC
    assert 'data-latched={latched ? "true" : "false"}' in DOC

    down = DOC[DOC.index("function micDown"):DOC.index("function micUp")]
    assert "if (latched) return;" in down

    up = DOC[DOC.index("function micUp"):DOC.index("function micLeave")]
    # Click-to-stop while latched.
    assert "setLatched(false);" in up and "micStop();" in up
    # A confirmed double-tap latches on WITHOUT calling micStop - the
    # recording must carry through, never stop between the two taps.
    latch_branch = up[up.index("isSecondTap"):up.index("Might be the first tap")]
    assert "setLatched(true);" in latch_branch
    assert "micStop()" not in latch_branch
    # An ordinary (non-double-tap) release still schedules the same
    # micStop hold-to-talk behavior - it's deferred, not skipped.
    assert "micStop();" in up[up.index("Might be the first tap"):]

    leave_start = DOC.index("function micLeave")
    leave = DOC[leave_start:DOC.index("return (", leave_start)]
    assert "if (!latched) micStop();" in leave


def test_mic_stops_on_unmount_not_just_on_release():
    """R3 cross-review defect 3 (MEDIUM): a latched (or mid-grace-window)
    recording has no release event left to stop it if the composer
    unmounts instead - the cleanup must run once, at true unmount
    ([] deps), and read refs directly rather than the `recording` state
    (which a [] -deps closure would otherwise freeze at its first-render
    value forever)."""
    leave_start = DOC.index("function micLeave")
    cleanup_start = DOC.index("React.useEffect", leave_start)
    cleanup_end = DOC.index("return (", cleanup_start)
    cleanup = DOC[cleanup_start:cleanup_end]
    assert re.search(r"\}\s*,\s*\[\s*\]\s*\)\s*;", cleanup), (
        "the cleanup effect must have an empty ([]) dependency array"
    )
    assert "micClearPendingStop();" in cleanup
    assert "recRef.current.stop();" in cleanup
    assert "streamRef.current" in cleanup and "getTracks" in cleanup
    # Not the `recording` state variable - see the docstring above.
    effect_body = cleanup[:cleanup.index("}, [])")]
    assert "recording" not in effect_body


def test_status_strip_carries_interrupt():
    assert 'data-testid="nv-interrupt"' in DOC
    assert 'data-testid="nv-stop"' in DOC


def test_decision_card_renders_routing_and_rejects_with_feedback():
    assert "approvers" in DOC, "routing renders from the item (P6 field)"
    assert 'data-testid="nv-reject-reason"' in DOC
    assert "SH_api.approve" in DOC and "SH_api.reject" in DOC


def test_decision_card_routing_pins_the_three_approver_shapes():
    """US-008 R3 item 3: the routing line reads item.approvers.{kind,
    roles,users} straight from the API's ApproverSpec.model_dump() (see
    primer/api/routers/workspaces.py's two "approvers": metadata.get(
    "approvers") sites and primer/agent/tool_manager.py's
    approvers.model_dump()) - pinned against the REAL model so a field
    rename on either side fails this test, not just a silent mismatch.
    """
    from typing import get_args

    from primer.model.tool_approval import ApproverSpec

    assert set(ApproverSpec.model_fields) == {"kind", "roles", "users"}
    assert set(get_args(ApproverSpec.model_fields["kind"].annotation)) == {
        "anyone", "roles", "users",
    }

    card = DOC[DOC.index("function NV_DecisionCard"):
               DOC.index("function NV_AskCard")]
    assert 'item.approvers.kind === "anyone"' in card
    assert 'item.approvers.kind === "roles"' in card
    assert "item.approvers.roles" in card
    assert "item.approvers.users" in card
    # No approvers at all (older parked rows, or a policy that never set
    # one) reads as "anyone", matching the router's own "None = anyone"
    # comment on both yields endpoints.
    assert '"anyone may decide"' in card


def test_ask_card_answers_by_tool_call_id():
    assert "SH_api.answer(item.sessionId, item.toolCallId" in DOC


def test_trace_is_a_split_not_an_overlay():
    assert "nv-trace-split" in DOC
    trace = DOC[DOC.index("function NV_TraceSplit"):]
    trace = trace[:trace.index("function NV_Composer")]
    assert "nv-scrim" not in trace, "the trace opens BESIDE the transcript"
    assert "SH_api.timeline" in DOC


def test_queued_steers_render_with_dismiss():
    assert "pending_messages" in DOC or "pending.map" in DOC
    assert "dismissQueuedSteer" in DOC


def test_rewind_and_compact_are_wired_not_gated():
    # US-008 R3 item 4: both the overflow menu row and the palette verb
    # now call the real endpoints - the S1 P2 "needs the endpoint" gate
    # from earlier rounds is gone. Scoped to NV_SessionHeader specifically
    # (item 2 registered a palette "Rewind Session"/"Compact Session"
    # verb elsewhere in this file, so an unscoped search would land on
    # the wrong "Rewind"/"Compact" occurrence).
    header = DOC[DOC.index("function NV_SessionHeader"):
                 DOC.index("function NV_Thought")]
    rewind = re.search(r"Rewind[\s\S]{0,200}", header)
    assert rewind and "disabled" not in rewind.group(0)
    assert "props.onOpenRewind" in header
    compact = re.search(r"Compact[\s\S]{0,200}", header)
    assert compact and "disabled" not in compact.group(0)
    assert "props.onCompact" in header
    # Both call the real backend, not a stub - and Rewind opens a picker
    # (notes 2.4: "radio list of user turns") rather than firing blind.
    assert "SH_api.compact(wid, sid)" in DOC
    assert "SH_api.rewind(wid, sid, toSeq)" in DOC
    assert "NV_RewindPicker" in DOC
    assert 'data-testid="nv-rewind-confirm"' in DOC
    assert 'data-testid="nv-rewind-cancel"' in DOC


def test_rewind_candidates_match_check_rewind_target_via_mini_racer():
    """R3 cross-review defect 2 (HIGH): NV_rewindCandidates must mirror
    primer/session/rewind.py's check_rewind_target exactly, or the picker
    offers choices the backend 422/409s on. The prior implementation
    computed "newest" as the raw max seq across ALL records (counting the
    rewind_marker's own seq) and treated the marker's to_seq as a LOWER
    bound excluding seq <= to_seq - backwards from the real rule (to_seq
    is only a floor relative to the latest COMPACTION marker; a prior
    rewind's own cut is enforced by simple absence from the visible set,
    not a separate floor). That inversion offered the DISCARDED span
    (seq 6-10 below) as valid targets while excluding seq 1, which is
    actually the only legal (and clearest) choice.

    NV_rewindCandidates is a pure function (no React/JSX), so its source
    is extracted and evaluated directly alongside the real
    session-adapter.jsx (SA_visibleRecords is its one dependency) -
    mirrors the reviewer's own MiniRacer repro rather than only
    source-grepping the fix.
    """
    from py_mini_racer import MiniRacer

    adapter_src = (CONSOLE.parent / "session-adapter.jsx").read_text(
        encoding="utf-8"
    )
    start = DOC.index("function NV_rewindCandidates")
    end = DOC.index("function NV_RewindPicker")
    candidates_src = DOC[start:end]

    ctx = MiniRacer()
    ctx.eval("var window = {};")
    ctx.eval(adapter_src)
    ctx.eval(candidates_src)
    ctx.eval(
        """
        var records = [
          {seq: 1, kind: "user_input", payload: {text: "one"}, created_at: "t1"},
          {seq: 2, kind: "assistant_token", payload: {text: "a1"}, created_at: "t2"},
          {seq: 3, kind: "user_input", payload: {text: "three"}, created_at: "t3"},
          {seq: 4, kind: "assistant_token", payload: {text: "a2"}, created_at: "t4"},
          {seq: 5, kind: "user_input", payload: {text: "five"}, created_at: "t5"},
          {seq: 6, kind: "assistant_token", payload: {text: "a3"}, created_at: "t6"},
          {seq: 7, kind: "user_input", payload: {text: "seven"}, created_at: "t7"},
          {seq: 8, kind: "assistant_token", payload: {text: "a4"}, created_at: "t8"},
          {seq: 9, kind: "user_input", payload: {text: "nine"}, created_at: "t9"},
          {seq: 10, kind: "assistant_token", payload: {text: "a5"}, created_at: "t10"},
          {seq: 11, kind: "rewind_marker", payload: {to_seq: 5, actor: "user"}, created_at: "t11"}
        ];
        var out = NV_rewindCandidates(records);
        """
    )
    import json

    seqs = json.loads(ctx.eval(
        "JSON.stringify(out.map(function (c) { return c.seq; }))"
    ))
    # seq 7 and 9 are inside the span the rewind discarded (> to_seq=5,
    # < the marker's own seq=11) and must not be offered. seq 1 is the
    # clearest legal target (strictly before the newest VISIBLE record,
    # 5) and must be. seq 5 itself is excluded too - it is the newest
    # visible record, and check_rewind_target rejects "nothing to
    # discard" targets the same way.
    assert 1 in seqs
    assert 3 in seqs
    assert 5 not in seqs
    assert 7 not in seqs
    assert 9 not in seqs


def test_file_doc_keeps_the_etag_discipline():
    assert "412" in FDOCS and "nv-file-conflict" in FDOCS
    assert re.search(r"fileWrite\(con\.wid, path, draft, force \? null : etag\)",
                     FDOCS)
    assert "NV_FILE_EDIT_MAX_BYTES" in FDOCS


def test_diff_lines_are_toned():
    assert 'data-tone={tone}' in FDOCS
