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
    m = re.search(r"onstop[\s\S]{0,700}", DOC)
    assert m and "setVal" in m.group(0) and "send()" not in m.group(0), (
        "dictation lands as editable text, never auto-sends"
    )


def test_status_strip_carries_interrupt():
    assert 'data-testid="nv-interrupt"' in DOC
    assert 'data-testid="nv-stop"' in DOC


def test_decision_card_renders_routing_and_rejects_with_feedback():
    assert "approvers" in DOC, "routing renders from the item (P6 field)"
    assert 'data-testid="nv-reject-reason"' in DOC
    assert "SH_api.approve" in DOC and "SH_api.reject" in DOC


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


def test_rewind_and_compact_stay_honestly_gated():
    m = re.search(r"Rewind[\s\S]{0,200}", DOC)
    assert m and "disabled" in m.group(0), (
        "rewind renders disabled until the S1 P2 endpoint exists"
    )


def test_file_doc_keeps_the_etag_discipline():
    assert "412" in FDOCS and "nv-file-conflict" in FDOCS
    assert re.search(r"fileWrite\(con\.wid, path, draft, force \? null : etag\)",
                     FDOCS)
    assert "NV_FILE_EDIT_MAX_BYTES" in FDOCS


def test_diff_lines_are_toned():
    assert 'data-tone={tone}' in FDOCS
