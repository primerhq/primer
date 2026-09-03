"""The Studio center (wiring plan P2 T7): doc host tabs, the session
doc's inherited data layer + prototype render, cards, the trace SPLIT
(never an overlay), the composer discipline.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
CONSOLE = ROOT / "ui" / "components" / "console"
# RETARGET (uiv2 R2 cutover, US-011a): nv-doc-host.jsx retired; tab
# semantics now live in nv-tab-groups.jsx and the empty-state prompt in
# nv-studio.jsx's NV_renderStudioDoc.
TABS = (CONSOLE / "nv-tab-groups.jsx").read_text(encoding="utf-8")
STUDIO = (CONSOLE / "nv-studio.jsx").read_text(encoding="utf-8")
DOC = (CONSOLE / "nv-session-doc.jsx").read_text(encoding="utf-8")
FDOCS = (CONSOLE / "nv-file-docs.jsx").read_text(encoding="utf-8")
TURNS = (UI / "foundation" / "shell-turns.js").read_text(encoding="utf-8")
STATUS = (UI / "foundation" / "shell-status.js").read_text(encoding="utf-8")
ATTENTION = (UI / "foundation" / "shell-attention.js").read_text(encoding="utf-8")
STYLES = (UI / "styles.css").read_text(encoding="utf-8")


def test_tabs_have_vscode_semantics():
    assert 'data-preview=' in TABS
    assert "onDoubleClick" in TABS and "promoteTab" in TABS
    assert "nv-tg-tab-close" in TABS


def test_center_empty_is_a_prompt_with_actions():
    m = re.search(r'data-testid="nv-center-empty"[\s\S]{0,900}', STUDIO)
    assert m and "session.create" in m.group(0)


def test_session_doc_reuses_the_pure_modules():
    for mod in ("SA_toTranscript", "SH_nestSubagentRows",
                "SH_collapseTurns", "SH_toolChipLabel",
                "SH_statusFromTap", "SH_scrollDecision"):
        assert mod in DOC, mod


def test_session_state_chip_always_visible_in_the_header():
    """Phase 2 (01a04ddf): the ONE served session_state, rendered as an
    always-visible header chip right beside the binding chip - unlike the
    bottom status strip (which only shows while a turn is actively
    running), this must never be conditionally omitted."""
    m = re.search(r"<NV_BindingChip[\s\S]{0,200}", DOC)
    assert m and "<NV_SessionStateChip" in m.group(0), (
        "the chip must sit right beside the binding chip, unconditionally "
        "rendered - not gated behind a truthy check"
    )
    chip = DOC[DOC.index("NV_SESSION_STATE_LABEL"):
               DOC.index("function NV_SessionHeader")]
    assert 'data-testid="nv-session-state-chip"' in chip
    for state in ("waiting", "running", "parked", "ended"):
        assert state in chip


def test_usage_meter_has_stable_testids_for_e2e():
    """Dogfood round 2: added for Playwright coverage of the null-vs-real
    context_length rendering - there was no hook to target the meter
    reliably before this."""
    header = DOC[DOC.index("function NV_SessionHeader"):
                 DOC.index("function NV_Thought")]
    assert 'data-testid="nv-usage"' in header
    assert 'data-testid="nv-usage-label"' in header
    assert "data-pct={pct}" in header


def test_thought_seeds_open_from_live_expanded_and_reopens_on_it():
    """Phase 2 (01a04ddf): "thinking tokens stream live" - the CURRENTLY
    streaming reasoning part renders expanded by default (liveExpanded),
    not the historical collapsed-by-default state every other reasoning
    block gets."""
    thought = DOC[DOC.index("function NV_Thought"):DOC.index("function NV_ToolBlock")]
    assert "React.useState(!!props.liveExpanded)" in thought
    assert "props.liveExpanded" in thought and "setOpen(true)" in thought


def test_error_surfaces_prefer_the_rfc7807_detail_over_the_bare_title():
    """Dogfood round 2 SEV add-on: the console's error toasts/banners
    read err.message, which ApiError (ui/foundation/api.js) sets from
    the problem+json "title" only ("Conflict") - the actual explanation
    the backend already threads through (exc.message -> "detail",
    primer/api/errors.py) never rendered, so a real 409 showed the user
    a bare "Conflict (req-...)" with no idea why. Every ApiError-
    consuming site in this file must prefer err.detail, matching the
    established `e.detail || e.message` convention nv-platform.jsx /
    nv-system.jsx already used elsewhere - counting occurrences (rather
    than a single "is present anywhere" check) catches a site that was
    missed or a new one added the old way."""
    assert DOC.count("err.detail || err.message") >= 9
    # The two sites with an extra null-guard (err may be undefined) keep
    # their guard but still prefer detail once err exists.
    assert "(err && (err.detail || err.message))" in DOC
    assert "(err && err.message)" not in DOC, (
        "a guarded site reverted to title-only"
    )
    # The send-failure path feeds BOTH the inline banner (nv-composer-
    # error) and the toast - the SEV's own report was exactly this path
    # (a rewind's 409 on a busy session).
    send_fn = DOC[DOC.index("function send() {"):]
    send_fn = send_fn[:send_fn.index("\n  function micStart")]
    assert '(err && (err.detail || err.message)) || "Steer failed"' in send_fn


def test_composer_never_locks_and_dictation_never_sends():
    # RETARGET (UX reconcile wave 4, audit A item 11): the old generic
    # "Send a message, Enter queues mid-run" placeholder covered every
    # non-terminal state; the mid-run steer/queue behavior it described
    # now has its own dedicated placeholder instead - see
    # test_composer_placeholder_per_state below for the full table.
    assert "Steer mid-run — queues to the turn boundary" in DOC
    # rec\.onstop = function specifically (not just the word "onstop") -
    # the R3 review's mic-unmount-cleanup fix added a prose comment that
    # also happens to say "onstop", which an unscoped search landed on
    # instead of the real handler.
    m = re.search(r"rec\.onstop = function[\s\S]{0,700}", DOC)
    assert m and "setVal" in m.group(0) and "send()" not in m.group(0), (
        "dictation lands as editable text, never auto-sends"
    )


def test_composer_placeholder_per_state():
    """Wave 4 (audit A item 11): only two forms existed before (terminal
    vs one generic form for everything else) - the reference screenshots
    show distinct copy per state instead. Final table:
      terminal -> "Send to reopen this session…" (unchanged)
      running (no gate item)  -> "Steer mid-run — queues to the turn
        boundary" (exact reference copy)
      idle / parked (any other case, incl. a gate item pending even
        while turn_status still reads "running") -> "Message {agentName}…"
        - both reference screenshots of a parked session show this exact
        "Message {agent}…" form (not distinct parked copy), so idle and
        parked share one branch rather than inventing a third string the
        reference never shows.
    """
    start = DOC.index("placeholder={props.terminal")
    end = DOC.index("onChange={function (ev) {", start)
    ternary = DOC[start:end]
    assert '"Send to reopen this session…"' in ternary
    assert "props.running && !props.waitNote" in ternary
    assert '"Steer mid-run — queues to the turn boundary"' in ternary
    assert '"Message " + (props.agentName || "agent") + "…"' in ternary

    # agentName is threaded from the same agentId this file already
    # computes for the byline (session.binding.agent_id/graph_id) - not
    # a new lookup.
    assert "agentName={agentId}" in DOC


def test_thought_label_via_mini_racer():
    """Wave 4 (audit A item 3): NV_Thought's collapsed label was the
    literal word "thought" plus a raw 110-char peek - the reference
    shows short semantic summaries ("Explored the repo", "Chose the
    handler seam"). True summarization needs an LLM (out of scope, not
    built here); SH_thoughtLabel is an honest heuristic - the first
    sentence, ellipsized ~60 chars, falling back to "thought" when
    empty/whitespace."""
    from py_mini_racer import MiniRacer

    start = TURNS.index("function SH_thoughtLabel")
    end = TURNS.index("\n}\n", start) + len("\n}\n")
    ctx = MiniRacer()
    ctx.eval(TURNS[start:end])

    assert ctx.call("SH_thoughtLabel", "Explored the repo.") == "Explored the repo."
    assert ctx.call(
        "SH_thoughtLabel", "Chose the handler seam\nread src/api.ts more closely",
    ) == "Chose the handler seam"

    long_sentence = (
        "This is a very long opening sentence that goes well past the "
        "sixty character budget the reference summaries stay under."
    )
    label = ctx.call("SH_thoughtLabel", long_sentence)
    assert len(label) == 60
    assert label.endswith("…")
    assert label == long_sentence[:59] + "…"

    assert ctx.call("SH_thoughtLabel", "") == "thought"
    assert ctx.call("SH_thoughtLabel", "   \n\t  ") == "thought"
    assert ctx.call("SH_thoughtLabel", None) == "thought"


def test_reasoning_uses_the_heuristic_label_not_a_raw_peek():
    assert "SH_thoughtLabel(text)" in DOC
    thought = DOC[DOC.index("function NV_Thought"):DOC.index("function NV_ToolBlock")]
    assert '>\n        thought\n' not in thought, (
        "the static literal word must be gone, not just supplemented"
    )
    assert "nv-thought-peek" not in thought
    assert "nv-thought-peek" not in STYLES, "dead CSS left behind"


def test_streaming_cursor_at_the_text_tail():
    """Wave 4 (audit A item 8): a blinking cursor at the tail of live-
    streaming assistant text. Judgment call (per the brief): reasoning
    live-parts do NOT get a cursor - NV_Thought is collapsed/muted by
    design (its own long-standing comment), and the reference never
    shows one mid-stream there; adding a blinking element to an
    intentionally muted, collapsed surface would fight that design
    rather than match the reference.
    """
    start = DOC.index('<div className="nv-turn-text md-body"')
    end = DOC.index("(row.children || []).map(function (child) {", start)
    turn_text = DOC[start:end]
    assert 'data-streaming={row.payload && row.payload.streaming' in turn_text

    thought = DOC[DOC.index("function NV_Thought"):DOC.index("function NV_ToolBlock")]
    assert "data-streaming" not in thought, (
        "reasoning stays muted/collapsed - no blinking cursor there "
        "(judgment call). Phase 2 (01a04ddf) added a DIFFERENT live-mid-"
        "stream affordance (auto-expand while it's the active thinking "
        "part, via liveExpanded) - that is not a cursor and does not "
        "violate this design call, so this assertion narrowed to the "
        "actual cursor markup instead of the bare word."
    )

    rule = STYLES[STYLES.index('.nv-turn-text.md-body[data-streaming="true"]'):]
    rule = rule[:rule.index("@keyframes nv-stream-cursor-blink")]
    assert "animation:" in rule
    assert ":last-child::after" in rule
    # No bespoke reduced-motion override needed - pin that the file's
    # existing global rule actually covers *::after (this animation's
    # target), so a future refactor of that global rule cannot silently
    # stop covering this cursor without a test noticing.
    global_rule = STYLES[STYLES.index("Global reduced-motion"):]
    global_rule = global_rule[:global_rule.index("}\n}") + 3]
    assert "*::after" in global_rule and "animation-duration" in global_rule


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


def test_derive_status_from_records_mirrors_the_live_frame_derivation():
    """User-reported refresh bug, round 2 (2026-08-29): the store's live
    status channel (session-store.js's SS_updateStatus) only knows the
    CURRENT verb ("running: grep_src", "thinking") from a live tap frame -
    a fresh page has none until the next one arrives, which during a long
    tool call can be minutes away. NV_deriveStatusFromRecords replays the
    durable records the REST history poll already fetched instead, using
    the exact same kind-based rule SS_updateStatus applies to live frames,
    so a refresh gets the real verb immediately rather than a bare
    turn_status label.

    Pure function (no React/JSX) - same MiniRacer-direct style as
    NV_rewindCandidates below rather than only source-grepping.
    """
    import json

    from py_mini_racer import MiniRacer

    start = DOC.index("function NV_deriveStatusFromRecords")
    end = DOC.index("\n}\n", start) + len("\n}\n")
    derive_src = DOC[start:end]

    ctx = MiniRacer()
    ctx.eval(derive_src)

    # An in-flight tool call (no matching tool_result/done yet) - the
    # rich "running: {tool}" case the user's screenshot expects.
    ctx.eval(
        """
        var out = NV_deriveStatusFromRecords([
          {seq: 1, kind: "user_input", created_at: "2026-08-29T00:00:00Z"},
          {seq: 2, kind: "reasoning", created_at: "2026-08-29T00:00:01Z"},
          {seq: 3, kind: "tool_call", created_at: "2026-08-29T00:00:02Z",
           payload: {name: "grep_src"}},
        ]);
        """
    )
    out = json.loads(ctx.eval("JSON.stringify(out)"))
    assert out["verb"] == "grep_src"
    assert out["startedMs"] == 1787961602000

    # Between turns: the last completed turn ended with "done" - no
    # activity in flight, so nothing to derive (the rowBusy fallback in
    # nv-session-doc.jsx falls through to the plain turn_status label).
    ctx.eval(
        """
        out = NV_deriveStatusFromRecords([
          {seq: 1, kind: "user_input", created_at: "2026-08-29T00:00:00Z"},
          {seq: 2, kind: "tool_call", created_at: "2026-08-29T00:00:01Z",
           payload: {name: "grep_src"}},
          {seq: 3, kind: "tool_result", created_at: "2026-08-29T00:00:02Z"},
          {seq: 4, kind: "done", created_at: "2026-08-29T00:00:03Z"},
        ]);
        """
    )
    assert ctx.eval("out") is None

    # A fresh user turn with no tool call yet - "thinking", same label
    # SS_updateStatus gives a live user_input frame.
    ctx.eval(
        """
        out = NV_deriveStatusFromRecords([
          {seq: 1, kind: "tool_call", created_at: "2026-08-29T00:00:00Z",
           payload: {name: "grep_src"}},
          {seq: 2, kind: "tool_result", created_at: "2026-08-29T00:00:01Z"},
          {seq: 3, kind: "done", created_at: "2026-08-29T00:00:02Z"},
          {seq: 4, kind: "user_input", created_at: "2026-08-29T00:00:10Z"},
        ]);
        """
    )
    out2 = json.loads(ctx.eval("JSON.stringify(out)"))
    assert out2["verb"] == "thinking"
    assert out2["startedMs"] == 1787961610000

    assert ctx.eval("NV_deriveStatusFromRecords([])") is None
    assert ctx.eval("NV_deriveStatusFromRecords(null)") is None


def test_last_turn_start_ms_avoids_the_idle_gap_between_turns():
    """Review finding (2026-08-29): session.last_turn_at stamps when the
    PREVIOUS turn completed, not the current one's start - for a session
    idle between turns (user stepped away, came back, sent a message), a
    mid-turn refresh using last_turn_at as startedMs would show elapsed =
    idle-gap + current-turn-time (e.g. "running - 14432s" after a lunch
    break), worse than resetting to 0. NV_lastTurnStartMs prefers the
    current turn's own trigger - the most recent user_input record, when
    it landed after the last turn ended - over that stale timestamp.
    """
    from py_mini_racer import MiniRacer

    start = DOC.index("function NV_lastTurnStartMs")
    end = DOC.index("\n}\n", start) + len("\n}\n")
    src = DOC[start:end]

    ctx = MiniRacer()
    ctx.eval(src)

    # Idle gap: last_turn_at is a lunch break ago, but a fresh user_input
    # landed after it - the fresh message wins, not the stale timestamp.
    ms = ctx.eval(
        """
        NV_lastTurnStartMs(
          [{kind: "user_input", created_at: "2026-08-29T12:00:00Z"}],
          {last_turn_at: "2026-08-29T08:00:00Z", created_at: "2026-08-28T00:00:00Z"}
        )
        """
    )
    assert ms == 1788004800000  # 2026-08-29T12:00:00Z, NOT the 08:00 gap

    # No user_input newer than last_turn_at (a continuation/steer with no
    # fresh message) - last_turn_at is still the best available signal.
    ms2 = ctx.eval(
        """
        NV_lastTurnStartMs(
          [{kind: "user_input", created_at: "2026-08-29T06:00:00Z"}],
          {last_turn_at: "2026-08-29T08:00:00Z", created_at: "2026-08-28T00:00:00Z"}
        )
        """
    )
    assert ms2 == 1787990400000  # 2026-08-29T08:00:00Z (last_turn_at)

    # Neither user_input nor last_turn_at - created_at is the tail.
    ms3 = ctx.eval(
        """
        NV_lastTurnStartMs([], {last_turn_at: null, created_at: "2026-08-28T00:00:00Z"})
        """
    )
    assert ms3 == 1787875200000  # 2026-08-28T00:00:00Z (created_at)


def test_status_strip_rebuilds_elapsed_time_from_server_state_on_refresh():
    """Refresh bug (2026-08-29): a reload mid-run has no tap history and
    no optimistic flag, so the busy indicator falls back to the polled
    session row's turn_status. The prior fallback stamped startedMs as
    Date.now() the moment THIS component instance first noticed rowBusy -
    every refresh reset the elapsed counter to ~0, which read as "lost
    the running indicator" even though the dot+label stayed visible. The
    fix rebuilds startedMs via NV_lastTurnStartMs (the idle-gap-safe
    fallback chain) so elapsed time survives a refresh instead of
    restarting from the reload moment - and prefers
    NV_deriveStatusFromRecords's real verb/tool over the bare turn_status
    label whenever the already-fetched records carry one.

    The rowBusy fallback is a few inline statements, not an extracted
    pure function (unlike NV_rewindCandidates below) - sliced out by its
    anchoring var declarations and evaluated directly, mirroring that
    test's own MiniRacer approach rather than only source-grepping.
    """
    import json

    from py_mini_racer import MiniRacer

    over_start = DOC.index("function NV_sessionIsOver")
    over_end = DOC.index("\n}\n", over_start) + len("\n}\n")
    is_over_src = DOC[over_start:over_end]

    derive_start = DOC.index("function NV_deriveStatusFromRecords")
    derive_end = DOC.index("\n}\n", derive_start) + len("\n}\n")
    derive_src = DOC[derive_start:derive_end]

    start_ms_start = DOC.index("function NV_lastTurnStartMs")
    start_ms_end = DOC.index("\n}\n", start_ms_start) + len("\n}\n")
    start_ms_src = DOC[start_ms_start:start_ms_end]

    start = DOC.index("var rowBusy = !!(session")
    end = DOC.index("var degraded = !!(gatesSnap")
    snippet_src = DOC[start:end]

    ctx = MiniRacer()
    ctx.eval(is_over_src)
    ctx.eval(derive_src)
    ctx.eval(start_ms_src)
    ctx.eval(
        """
        var shown = null;
        var store = { recordsBySeq: [] };
        var session = {
          status: "running",
          turn_status: "running",
          last_turn_at: "2026-08-29T00:00:00Z",
          created_at: "2026-08-28T00:00:00Z",
        };
        var effectivePhase = null;
        """
        + snippet_src
    )
    result = json.loads(ctx.eval("JSON.stringify(shown)"))
    assert result["verb"] == "running", (
        "with no records to replay and no served agent_phase either, the "
        "fallback is the plain turn_status label"
    )
    assert result["startedMs"] == 1787961600000, (
        "startedMs must equal Date.parse(session.last_turn_at), not a "
        "client-observed timestamp"
    )

    # Records ARE available (the REST history poll already resolved) and
    # carry an in-flight tool call - the strip must show the real tool,
    # not the generic turn_status label, and time it from the tool_call's
    # own created_at rather than last_turn_at.
    ctx.eval(
        """
        shown = null;
        store.recordsBySeq = [
          {seq: 1, kind: "user_input", created_at: "2026-08-29T00:01:00Z"},
          {seq: 2, kind: "tool_call", created_at: "2026-08-29T00:01:05Z",
           payload: {name: "grep_src"}},
        ];
        """
        + snippet_src
    )
    result2 = json.loads(ctx.eval("JSON.stringify(shown)"))
    assert result2["verb"] == "grep_src"
    assert result2["startedMs"] == 1787961665000

    # Phase 2 (01a04ddf): no records to replay yet (e.g. the SEV-2
    # dual-write gap - the first instruction's user_input record hasn't
    # landed), but agent_phase IS being served - the real signal must
    # win over the bare, uninformative turn_status string ("running:
    # running" read as broken, live finding 2026-08-29).
    ctx.eval(
        """
        shown = null;
        store.recordsBySeq = [];
        effectivePhase = "thinking";
        """
        + snippet_src
    )
    result3 = json.loads(ctx.eval("JSON.stringify(shown)"))
    assert result3["verb"] == "thinking", (
        "agent_phase must win over the bare turn_status label when no "
        "record replay answers directly"
    )

    # No last_turn_at yet (session on its first-ever turn) - created_at
    # is the fallback, never Date.now() while either timestamp exists.
    ctx.eval(
        """
        shown = null;
        store.recordsBySeq = [];
        session.last_turn_at = null;
        """
        + snippet_src
    )
    started_ms2 = ctx.eval("shown.startedMs")
    assert started_ms2 == 1787875200000

    # A session that is not busy (idle, or over) must not synthesize a
    # status at all - shown stays whatever it already was (null here).
    ctx.eval(
        """
        shown = null;
        session.turn_status = "idle";
        """
        + snippet_src
    )
    assert ctx.eval("shown") is None


def test_effective_phase_prefers_live_frame_then_served_field_fenced_by_turn_no():
    """Phase 2 (01a04ddf): effectivePhase must (1) prefer the live
    PhaseFrame tap delta when its turn_no matches the CURRENT turn (lower
    latency than the poll), (2) fall back to the session row's OWN served
    agent_phase when the live frame is missing or stale (a fresh mount
    with no tap history yet - the acceptance invariant's whole point:
    refresh must render from served state, not a live frame), and (3)
    ignore either signal when its turn_no belongs to a turn that already
    ended (both fields are additive/optional and only meaningful while
    THIS turn is running - primer/model/workspace_session.py:628-661)."""
    import json

    from py_mini_racer import MiniRacer

    start = DOC.index("var effectivePhase = null;")
    end = DOC.index("\n\n", start)
    snippet_src = DOC[start:end]

    ctx = MiniRacer()

    def run(session, phase_snap):
        ctx.eval(
            "var session = " + json.dumps(session) + ";"
            "var phaseSnap = " + json.dumps(phase_snap) + ";"
            + snippet_src
        )
        return ctx.eval("effectivePhase")

    # No session at all (not yet loaded) - no phase to show.
    assert run(None, None) is None

    # Fresh mount / hard refresh: no live frame has arrived yet, but the
    # session poll already carries agent_phase for the current turn - the
    # acceptance invariant's exact scenario.
    served = {"turn_no": 3, "agent_phase": "executing", "agent_phase_turn_no": 3}
    assert run(served, None) == "executing"

    # A live frame for the SAME turn wins over the served field (lower
    # latency - the poll can be up to 2s stale).
    assert run(served, {"phase": "responding", "turnNo": 3}) == "responding"

    # A live frame from a turn that already ended (delayed publish) must
    # be ignored, falling back to the served field for the CURRENT turn.
    assert run(served, {"phase": "responding", "turnNo": 2}) == "executing"

    # The served field itself stale (belongs to a prior turn) with no
    # live frame either - no phase to show, not a stale one.
    stale = {"turn_no": 4, "agent_phase": "executing", "agent_phase_turn_no": 3}
    assert run(stale, None) is None


def test_phase_indicator_suppressed_by_anything_more_specific():
    """Phase 2 (01a04ddf): the in-chat presence indicator only fills the
    gap nothing else already represents - see the pipeline useMemo's own
    comment for the full rationale per suppression rule."""
    import json

    from py_mini_racer import MiniRacer

    start = DOC.index("var lastFlat = flat.length")
    end = DOC.index("var liveFromSeq = Infinity;", start)
    snippet_src = DOC[start:end]

    ctx = MiniRacer()

    def indicator_pushed(flat, session, effective_phase):
        ctx.eval(
            "var flat = " + json.dumps(flat) + ";"
            "var session = " + json.dumps(session) + ";"
            "var effectivePhase = " + json.dumps(effective_phase) + ";"
            + snippet_src
        )
        pushed = json.loads(ctx.eval("JSON.stringify(flat)"))
        return pushed[-1]["kind"] == "phase_indicator" if pushed else False

    running = {"session_state": "running"}

    # The happy path: thinking, nothing else stands in for it yet.
    assert indicator_pushed([{"seq": 1, "kind": "user_message"}], running, "thinking")
    assert indicator_pushed([{"seq": 1, "kind": "user_message"}], running, "executing")

    # "responding" is replaced by the live streaming text row itself.
    assert not indicator_pushed(
        [{"seq": 1, "kind": "assistant_message"}], running, "responding")
    # "waiting" (between turns) gets no indicator.
    assert not indicator_pushed([{"seq": 1, "kind": "user_message"}], running, "waiting")
    # A live reasoning part already streaming IS the thinking indicator.
    assert not indicator_pushed([{"seq": 1, "kind": "reasoning"}], running, "thinking")
    # A tool call already in flight IS the executing indicator.
    assert not indicator_pushed([{"seq": 1, "kind": "tool_call"}], running, "executing")
    # No phase known at all - nothing to show.
    assert not indicator_pushed([{"seq": 1, "kind": "user_message"}], running, None)
    # Not genuinely running (parked/ended/waiting) - never paint a ghost
    # indicator off a stale phase value.
    assert not indicator_pushed(
        [{"seq": 1, "kind": "user_message"}], {"session_state": "parked"}, "thinking")
    # No session loaded yet.
    assert not indicator_pushed([{"seq": 1, "kind": "user_message"}], None, "thinking")


def test_decision_card_renders_routing_and_rejects_with_feedback():
    # RETARGET (wave 6, audit A item 14-routing): routing moved from an
    # inline item.approvers ternary to SH_routingLine(item, viewer) -
    # see test_decision_card_delegates_routing_to_the_shared_helper.
    assert "SH_routingLine(item" in DOC, "routing renders from the item (P6 field)"
    assert 'data-testid="nv-reject-reason"' in DOC
    assert "SH_api.approve" in DOC and "SH_api.reject" in DOC


def test_reject_button_always_reads_reject_with_feedback():
    """Wave 4 (audit A item 14 partial, a): the resting label used to be
    "Reject…" and only became "Reject with feedback" after the first
    click - the reference shows the enriched label at rest too. The
    second-click confirm flow (rejOpen gates whether the click opens the
    reason textarea vs actually submits) is UNCHANGED - only the label
    text stopped varying."""
    card = DOC[DOC.index("function NV_DecisionCard"):
               DOC.index("function NV_AskCard")]
    assert '}}>Reject with feedback</button>' in card
    assert "Reject…" not in card
    assert '{rejOpen ? "Reject with feedback" : "Reject…"}' not in card
    # The flow itself: first click only opens the reason box (no request
    # yet), second click actually calls reject.
    assert "if (!rejOpen) { setRej(true); return; }" in card


def test_decision_card_diff_preview_via_mini_racer():
    """Wave 4 (audit A item 14 partial, b): item.preview renders as flat
    text UNLESS it is diff-shaped (a unified-diff hunk header present),
    in which case it colors per-line like nv-file-docs.jsx's NV_DiffDoc
    already does for the Files tab - SH_diffLineTone/SH_looksLikeDiff
    (shell-turns.js) are a small duplicate of that one-line heuristic
    (that file/component is outside this task's boundary), pinned here
    against the real diff text from the reference screenshot."""
    from py_mini_racer import MiniRacer

    tone_start = TURNS.index("function SH_diffLineTone")
    tone_end = TURNS.index("\n}\n", tone_start) + len("\n}\n")
    looks_start = TURNS.index("function SH_looksLikeDiff")
    looks_end = TURNS.index("\n}\n", looks_start) + len("\n}\n")

    ctx = MiniRacer()
    ctx.eval(TURNS[tone_start:tone_end])
    ctx.eval(TURNS[looks_start:looks_end])

    diff_preview = (
        "@@ -4,6 +4,9 @@\n"
        "  export const webhookConfig = {\n"
        '+   signingSecret: env("STRIPE_WEBHOOK_SECRET"),\n'
        '+   rotatedAt: "2026-08-23T09:41:00Z",\n'
        '-   signingSecret: "whsec_live_9f31",\n'
        "    tolerance: 300,\n"
    )
    assert ctx.call("SH_looksLikeDiff", diff_preview) is True
    for line, tone in (
        ("@@ -4,6 +4,9 @@", "ctx"),
        ('+   signingSecret: env("STRIPE_WEBHOOK_SECRET"),', "add"),
        ('-   signingSecret: "whsec_live_9f31",', "del"),
        ("    tolerance: 300,", "ctx"),
    ):
        assert ctx.call("SH_diffLineTone", line) == tone, line

    # A false-positive guard: an ordinary bullet list starting with "-"
    # is not a diff just because a line starts with a hyphen.
    assert ctx.call(
        "SH_looksLikeDiff", "- first thing\n- second thing\n",
    ) is False
    assert ctx.call("SH_looksLikeDiff", "") is False


def test_decision_and_ask_cards_disable_when_the_session_has_ended():
    """Live finding 01a064d3: a park can outlive its session (a sweep/
    timeout continuation that then fails ends the session without
    clearing parked_status), so the decision/ask card could still render
    as if a human could act on it - live Approve/Reject/Answer buttons
    on an already-ended session, contradicting the header's own "Ended"
    pill. The ended state must win: keep the card for historical
    context, but its controls are disabled and annotated rather than
    live-looking."""
    decision = DOC[DOC.index("function NV_DecisionCard"):
                   DOC.index("function NV_AskCard")]
    assert 'data-testid="nv-approve" disabled={!!props.ended}' in decision
    # uiv2 Wave 3 (a-14 fold): nv-reject now ALSO guards on an empty
    # reason once the box is open (see test_reject_button_requires_a_
    # non_empty_reason_to_submit) - the ended check is one clause of a
    # combined disabled expression now, not the whole thing.
    assert "disabled={!!props.ended || (rejOpen && !reason.trim())}" in decision
    assert "session ended before this could be resolved" in decision

    ask = DOC[DOC.index("function NV_AskCard"):]
    assert 'data-testid="nv-ask-submit" disabled={!!props.ended}' in ask
    assert 'data-testid="nv-ask-answer" disabled={!!props.ended}' in ask
    assert "session ended before this could be answered" in ask

    # ended is threaded from the SAME NV_sessionIsOver check the fold
    # line and header pill already use - one source of truth, not a
    # second computation that could drift from it.
    assert "var ended = NV_sessionIsOver(session);" in DOC
    assert "<NV_DecisionCard key={item.id} item={item} ended={ended}" in DOC
    assert "<NV_AskCard key={item.id} item={item} ended={ended}" in DOC


def test_decision_card_uses_the_diff_helpers_only_when_diff_shaped():
    card = DOC[DOC.index("function NV_DecisionCard"):
               DOC.index("function NV_AskCard")]
    assert "SH_looksLikeDiff(item.preview)" in card
    assert "SH_diffLineTone(line)" in card
    assert "nv-card-preview-diff" in card and "nv-diff-lines" in card
    # The flat fallback (non-diff previews) must still exist untouched.
    assert '<pre className="nv-card-preview">{item.preview}</pre>' in card


def test_decision_card_delegates_routing_to_the_shared_helper():
    """Wave 6 (audit A item 14-routing): NV_DecisionCard's own inline
    routing ternary is retired - SH_routingLine (shell-attention.js,
    Dev-Backend's wave 3) is the SAME decision, now viewer-aware ("who
    may decide: {spec} — you qualify", dropped when the viewer does not
    qualify) instead of only ever naming the spec. This pins the WIRING
    (the delegation itself, plus viewer identity threading from con) -
    SH_routingLine's own string-exact behavior is already covered by
    Dev-Backend's tests/ui/test_shell_attention_model.py.
    """
    card = DOC[DOC.index("function NV_DecisionCard"):
               DOC.index("function NV_AskCard")]
    assert "SH_routingLine(item, { username: con.username, role: con.role })" in card
    # The old inline field-reading ternary is actually gone, not just
    # supplemented - two implementations of the same decision drifting
    # apart is exactly the failure mode delegating is meant to prevent.
    assert 'item.approvers.kind === "anyone"' not in card
    assert "awaiting " not in card


def test_routing_line_still_matches_the_real_approver_spec_model():
    """US-008 R3 item 3's original intent, preserved across the wave 6
    move: the routing decision must read fields that actually exist on
    the backend's ApproverSpec, pinned against the REAL model so a field
    rename on either side fails a test, not just a silent mismatch. The
    decision itself now lives in shell-attention.js (Dev-Backend, wave
    3) rather than inline here - this follows it there rather than
    re-implementing a second copy of the check against my own file.
    """
    from typing import get_args

    from primer.model.tool_approval import ApproverSpec

    assert set(ApproverSpec.model_fields) == {"kind", "roles", "users"}
    assert set(get_args(ApproverSpec.model_fields["kind"].annotation)) == {
        "anyone", "roles", "users",
    }

    routing_fn = ATTENTION[ATTENTION.index("function SH_routingLine"):]
    routing_fn = routing_fn[:routing_fn.index("\n}\n") + 3]
    assert 'approvers.kind === "roles"' in routing_fn
    assert "approvers.roles" in routing_fn and "approvers.users" in routing_fn
    assert '"anyone may decide"' in routing_fn


def test_ask_card_radio_options_via_mini_racer_and_wiring():
    """Wave 6 (audit A item 15 render half): the free-text textarea is
    now the fallback, not the only form - SH_askOptionsOf (shell-
    attention.js, wave 3) normalizes response_schema.enum into a radio
    list; item.responseSchema reads as undefined until wave 5's backend
    passthrough lands (defensive read, shell-attention.js's own comment),
    so this exercises the radio path with a SYNTHETIC schema rather than
    a live one - exactly the brief's "testable now" framing.
    """
    from py_mini_racer import MiniRacer

    start = ATTENTION.index("function SH_askOptionsOf")
    end = ATTENTION.index("\n}\n", start) + len("\n}\n")
    ctx = MiniRacer()
    ctx.eval(ATTENTION[start:end])

    opts = ctx.call(
        "SH_askOptionsOf",
        {"enum": ["Original charge currency", "Always USD", "Ask per refund"]},
    )
    assert [o["value"] for o in opts] == [
        "Original charge currency", "Always USD", "Ask per refund",
    ]
    assert ctx.call("SH_askOptionsOf", None) is None
    assert ctx.call("SH_askOptionsOf", {"type": "string"}) is None

    card = DOC[DOC.index("function NV_AskCard"):
               DOC.index("function NV_TraceSplit")]
    assert "SH_askOptionsOf(item.responseSchema)" in card
    assert 'data-testid="nv-ask-options"' in card
    assert 'data-selected={val === optVal ? "true" : "false"}' in card
    # Pre-selecting the first option is THIS component's own rendering
    # choice, deliberately not SH_askOptionsOf's (see that function's own
    # comment) - pin that the choice was actually made, not left as "".
    assert "options[0].value" in card
    # The free-text fallback (no schema, or a schema with no enum) must
    # still exist untouched.
    assert 'data-testid="nv-ask-answer"' in card


def test_ask_card_answers_by_tool_call_id():
    assert "SH_api.answer(item.sessionId, item.toolCallId" in DOC


def test_trace_is_a_split_not_an_overlay():
    """The persistent trace SIDEBAR itself must stay a side panel, not a
    modal - scoped to NV_TraceSplit's own body only (not the region up
    to NV_Composer), since dogfood round 2 added a genuinely separate,
    explicitly-requested scrim overlay (NV_TraceMaximize, the "maximize"
    view) right after it in this file; that one opening a scrim is by
    design, not a regression of this invariant."""
    assert "nv-trace-split" in DOC
    trace = DOC[DOC.index("function NV_TraceSplit"):]
    trace = trace[:trace.index("function NV_TraceMaximize")]
    assert "nv-scrim" not in trace, "the trace opens BESIDE the transcript"
    assert "SH_api.timeline" in DOC


def test_queued_steers_render_with_dismiss():
    assert "pending_messages" in DOC or "pending.map" in DOC
    assert "dismissQueuedSteer" in DOC


def test_queued_badge_is_uppercase_and_styled_not_just_text_transform():
    """Audit A item 9: nv-steer-mark already had text-transform: uppercase
    (a paint-only change - the DOM text stays "queued"), but none of the
    badge chrome (border/padding/radius) the reference shows. Reuses the
    established small-mono-badge treatment (.nv-filedoc-dirty) and the
    already-established "queued/waiting" amber tone (.pill-paused) rather
    than inventing a new one - pin both so a future edit cannot silently
    drop back to plain muted text."""
    rule = STYLES[STYLES.index(".nv-steer-mark {"):]
    rule = rule[:rule.index("}") + 1]
    assert "text-transform: uppercase" in rule
    assert "border" in rule and "var(--amber)" in rule
    assert "border-radius" in rule
    assert "padding" in rule


def test_composer_wait_note_uses_the_shared_parked_status_line():
    """Wave 2 addendum, revision (2026-08-29): the composer's waitNote used
    to compute a "parked, waiting on {session.waiting_reason}" line inline -
    session.waiting_reason is not a real WorkspaceSession field (Dev-
    Backend grepped primer/, it is always undefined), so that branch's
    only real output was the generic "a wake" fallback even during a real
    approval/ask park. SH_parkedStatusLine (shell-status.js, Dev-Backend's
    wave 1) already encapsulates the full decision - the swap here is a
    one-line wiring change, not new logic, so this pins the wiring plus
    the exact strings it now produces for the three cases the brief
    named, calling the REAL shell-status.js source (not a re-
    implementation) with gateItems shaped exactly like
    shell-attention.js's SH_toAttentionItems produces.
    """
    assert "waitNote={window.SH_parkedStatusLine(session, gateItems)}" in DOC
    assert "waiting_reason" not in DOC, (
        "the dead session.waiting_reason field read must be gone"
    )

    from py_mini_racer import MiniRacer

    wait_start = STATUS.index("function SH_waitLine")
    wait_end = STATUS.index("\n}\n", wait_start) + len("\n}\n")
    parked_start = STATUS.index("function SH_parkedStatusLine")
    parked_end = STATUS.index("\n}\n", parked_start) + len("\n}\n")

    ctx = MiniRacer()
    ctx.eval(STATUS[wait_start:wait_end])
    ctx.eval(STATUS[parked_start:parked_end])

    approval_gate = [{"kind": "approval", "gatedTool": "workspace__write_file"}]
    assert ctx.call(
        "SH_parkedStatusLine", {"parked_status": "parked"}, approval_gate,
    ) == "waiting on approval — workspace__write_file (parked, worker released)"

    ask_gate = [{"kind": "question", "gatedTool": "ask_user"}]
    assert ctx.call(
        "SH_parkedStatusLine", {"parked_status": "parked"}, ask_gate,
    ) == "waiting on your answer — ask_user (parked, worker released)"

    assert ctx.call(
        "SH_parkedStatusLine", {"parked_status": "parked"}, [],
    ) == "parked — waiting on a wake"

    assert ctx.call("SH_parkedStatusLine", {"parked_status": None}, []) is None


def test_byline_timestamps_render_from_created_at():
    """Audit A item 2: both bylines get a muted timestamp next to the
    name, from row.createdAt (already on every row - SA_toTranscript sets
    it from the record's own created_at). Uses SH_shortTime (shell-
    turns.js) rather than inventing a new formatter - same short local-
    time format shared/transcript.jsx's CT_formatTime already
    established elsewhere in the app."""
    user_start = DOC.index('if (row.kind === "user_message")')
    user_byline = DOC[user_start:DOC.index("nv-turn-text", user_start)]
    assert "nv-turn-time" in user_byline
    assert "SH_shortTime(row.createdAt)" in user_byline

    agent_start = DOC.index('data-testid={"nv-trace-open:" + row.seq}',
                             DOC.index("nv-turn nv-turn-agent"))
    agent_byline = DOC[DOC.index("nv-turn nv-turn-agent"):agent_start]
    assert "nv-turn-time" in agent_byline
    assert "SH_shortTime(row.createdAt)" in agent_byline


def test_short_time_formats_like_the_transcript_helper_via_mini_racer():
    """SH_shortTime (shell-turns.js) mirrors shared/transcript.jsx's
    CT_formatTime format exactly (hour:minute, no seconds) - that file is
    outside this task's boundary, so this is a small duplicate rather
    than a cross-file import, pinned to the same format so the two never
    silently drift apart."""
    from py_mini_racer import MiniRacer

    start = TURNS.index("function SH_shortTime")
    end = TURNS.index("\n}\n", start) + len("\n}\n")
    src = TURNS[start:end]

    ctx = MiniRacer()
    ctx.eval(src)
    assert ctx.eval("SH_shortTime('')") == ""
    assert ctx.eval("SH_shortTime(null)") == ""
    assert ctx.eval("SH_shortTime('not-a-date')") == ""
    formatted = ctx.eval("SH_shortTime('2026-08-29T14:05:00Z')")
    assert re.match(r"^\d{1,2}:\d{2}\s*[AaPp]?[Mm]?$", formatted), formatted


def test_trace_header_label_via_mini_racer():
    """Audit A item 5, amended by the live finding in 01a064d3:
    NV_TraceSplit's header enriches "trace · turn N" to "trace ·
    {calls} · {span}s" using the SAME timeline nodes the trace body
    renders below it (tool_call AND llm_call - both get a T/A glyph row
    per NV_traceGlyph). The header used to count only over the
    transcript's own turnRows, which SA_toTranscript deliberately never
    populates with llm_call records (session-adapter.jsx's
    SA_SKIP_IN_TRANSCRIPT), so a turn with any llm_call undercounted
    against what the body actually showed ("1 CALL" above three visible
    rows, live capture evidence). Span is the latest node end (ts +
    duration_ms) minus the earliest node start, not a sum of individual
    durations (a sum here would read 4s + 3s = 7s, not the correct 8s
    below), and a turn with zero tool/llm calls keeps the plain form
    exactly as before."""
    from py_mini_racer import MiniRacer

    start = TURNS.index("function SH_traceHeaderLabel")
    end = TURNS.index("\n}\n", start) + len("\n}\n")
    src = TURNS[start:end]

    ctx = MiniRacer()
    ctx.eval(src)

    # No tool/llm calls (just a graph boundary marker) - unchanged plain form.
    assert ctx.eval(
        'SH_traceHeaderLabel(3, [{kind: "node"}])'
    ) == "trace · turn 3"
    assert ctx.eval("SH_traceHeaderLabel(3, [])") == "trace · turn 3"

    # One call - singular "call", span from its own start + duration.
    one = ctx.eval(
        """
        SH_traceHeaderLabel(1, [
          {kind: "tool_call", ts: "2026-08-29T00:00:00Z", duration_ms: 4000},
        ])
        """
    )
    assert one == "trace · 1 call · 4s"

    # tool_call + llm_call (the exact shape that previously undercounted)
    # - plural, both count, span is last-end minus first-start across
    # ALL of them, not a sum.
    multi = ctx.eval(
        """
        SH_traceHeaderLabel(2, [
          {kind: "tool_call", ts: "2026-08-29T00:00:00Z", duration_ms: 4000},
          {kind: "llm_call", ts: "2026-08-29T00:00:05Z", duration_ms: 3000},
        ])
        """
    )
    assert multi == "trace · 2 calls · 8s"


def _trace_mini_racer_ctx():
    """Shared setup for the trace-row MiniRacer tests below: transpiles
    the helpers + NV_TraceLine + NV_TraceSplit + NV_TraceMaximize +
    NV_TraceRow as one block (function declarations are inert until
    called, so NV_TraceSplit/NV_TraceMaximize's own hook/fetch bodies
    never execute - only NV_TraceLine and NV_TraceRow are actually
    invoked below) against a stub React + SH_shortTime."""
    from py_mini_racer import MiniRacer

    from primer.api._jsx_bundle import JSXBundler

    bundler = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text(encoding="utf-8"),
    )
    start = DOC.index("function NV_traceElapsed")
    end = DOC.index("function NV_StatusStrip")
    transpiled = bundler._transform(DOC[start:end], "trace_row_test.jsx")

    ctx = MiniRacer()
    ctx.eval(
        """
        var __openValue = false;
        var React = {
          createElement: function (type, props) {
            var children = Array.prototype.slice.call(arguments, 2);
            return { type: type, props: props || {}, children: children };
          },
          useState: function (init) {
            return [__openValue, function () {}];
          }
        };
        function SH_shortTime(ts) { return ts ? "2:05 PM" : ""; }
        function SH_traceHeaderLabel() { return "trace"; }
        """
    )
    ctx.eval(transpiled)
    return ctx


def test_trace_line_is_one_line_and_never_expandable_via_mini_racer():
    """Dogfood round 2: the sidebar's own row (NV_TraceLine) is a plain
    div, always - it never becomes a button/toggle regardless of node
    kind, and it renders the literal "[T]"/"[A]" glyph plus timestamp
    plus label plus elapsed the ticket specifies. Real execution (not
    source-grepping) is what actually proves clicking a sidebar row
    can't do anything - a stray onClick would source-grep clean."""
    import json

    ctx = _trace_mini_racer_ctx()

    def render(node, depth, index, agent_name=None, is_graph=False):
        ctx.eval(
            "var __out = NV_TraceLine({node: " + json.dumps(node)
            + ", depth: " + json.dumps(depth) + ", index: " + json.dumps(index)
            + ", agentName: " + json.dumps(agent_name)
            + ", isGraph: " + json.dumps(is_graph) + "});"
        )
        return json.loads(ctx.eval("JSON.stringify(__out)"))

    tool = render(
        {"kind": "tool_call", "name": "bash", "ts": "2026-08-30T10:00:00Z",
         "duration_ms": 1200},
        0, 0,
    )
    line = tool["children"][0]
    assert line["type"] == "div", "the sidebar row must never be a button"
    assert "onClick" not in line["props"]
    tree = json.dumps(tool, ensure_ascii=False)
    assert '"T"' in tree
    assert "nv-trace-glyph" in tree
    assert '"data-kind": "tool"' in tree
    assert "bash" in tree
    assert "1s" in tree
    assert "nv-trace-args" not in tree, "the sidebar never shows arguments inline"

    agent = render(
        {"kind": "llm_call", "model": "gpt-4o", "duration_ms": 900}, 0, 1,
        agent_name="operator",
    )
    tree = json.dumps(agent, ensure_ascii=False)
    assert '"A"' in tree
    assert '"data-kind": "agent"' in tree
    assert "operator" in tree, "a plain agent-bound session shows its own agent name"


def test_trace_line_agent_name_falls_back_to_node_attribution_for_graphs():
    """A graph session has no single agent - the ticket's own fallback
    rule: node attribution (node_id) first, then the model string."""
    import json

    ctx = _trace_mini_racer_ctx()

    def render(node):
        ctx.eval(
            "var __out = NV_TraceLine({node: " + json.dumps(node)
            + ', depth: 0, index: 0, agentName: "checkout-graph", isGraph: true});'
        )
        return json.loads(ctx.eval("JSON.stringify(__out)"))

    with_node = render({"kind": "llm_call", "node_id": "validate-step", "model": "gpt-4o"})
    assert "validate-step" in json.dumps(with_node)

    without_node = render({"kind": "llm_call", "model": "gpt-4o"})
    tree = json.dumps(without_node, ensure_ascii=False)
    assert "gpt-4o" in tree
    assert "checkout-graph" not in tree, (
        "the raw graph id is not a real agent name - the model is a "
        "closer real answer than the graph's own id"
    )


def test_trace_row_expand_toggle_via_mini_racer():
    """The maximize overlay's own row (NV_TraceRow) IS a toggle now for
    both tool_call and llm_call kinds - expanding a tool_call shows BOTH
    its arguments and its paired result (timeline.py's tool_result
    branch now attaches a size-capped result alongside status/
    duration_ms), and expanding an llm_call shows its own call metadata.
    Same "stub React, run the real component" style as
    test_divider_renders_as_a_rule_not_a_bubble_via_mini_racer, extended
    to stub useState (NV_TraceRow's one hook) since a real render needs
    a controllable open/closed value, not just element-tree shape.
    """
    import json

    ctx = _trace_mini_racer_ctx()

    def render(node, depth, index, open_, agent_name=None, is_graph=False):
        ctx.eval("__openValue = " + ("true" if open_ else "false") + ";")
        ctx.eval(
            "var __out = NV_TraceRow({node: " + json.dumps(node)
            + ", depth: " + json.dumps(depth) + ", index: " + json.dumps(index)
            + ", agentName: " + json.dumps(agent_name)
            + ", isGraph: " + json.dumps(is_graph) + "});"
        )
        return json.loads(ctx.eval("JSON.stringify(__out)"))

    tool_call = {
        "kind": "tool_call", "name": "bash", "arguments": {"command": "ls"},
        "duration_ms": 12000,
        "result": {"output": "file1\nfile2", "error": False, "truncated": False},
    }

    closed = render(tool_call, 0, 0, open_=False)
    line = closed["children"][0]
    assert line["type"] == "button", "a tool_call row must be a toggle"
    tree = json.dumps(closed, ensure_ascii=False)
    assert "▸" in tree
    assert '"T"' in tree, "tool_call must get the [T] glyph"
    assert "nv-trace-args" not in tree, "collapsed must not render the detail block"

    opened = render(tool_call, 0, 0, open_=True)
    tree = json.dumps(opened, ensure_ascii=False)
    assert "▾" in tree
    assert '"command": "ls"' in tree or '\\"command\\": \\"ls\\"' in tree, (
        "arguments must be in the expanded body"
    )
    assert "file1" in tree, "the result must ALSO be in the expanded body"

    no_result_yet = render(
        {"kind": "tool_call", "name": "grep", "arguments": {}, "result": None},
        0, 1, open_=True,
    )
    assert "no result yet" in json.dumps(no_result_yet, ensure_ascii=False)

    llm = render(
        {"kind": "llm_call", "model": "gpt-4o", "profile_id": "mp-1",
         "input_tokens": 10, "output_tokens": 5},
        0, 2, open_=False, agent_name="operator",
    )
    line = llm["children"][0]
    assert line["type"] == "button", "llm_call is now ALSO expandable"
    tree = json.dumps(llm, ensure_ascii=False)
    assert '"A"' in tree
    assert "operator" in tree

    opened_llm = render(
        {"kind": "llm_call", "model": "gpt-4o", "profile_id": "mp-1",
         "input_tokens": 10, "output_tokens": 5},
        0, 2, open_=True, agent_name="operator",
    )
    tree = json.dumps(opened_llm, ensure_ascii=False)
    assert '"model": "gpt-4o"' in tree or '\\"model\\": \\"gpt-4o\\"' in tree
    assert '"profile_id": "mp-1"' in tree or '\\"profile_id\\": \\"mp-1\\"' in tree


def test_trace_split_uses_the_enriched_header_label():
    """NV_TraceSplit's header renders SH_traceHeaderLabel(turnNo, ...)
    rather than the bare "trace · turn N" string directly. Live finding
    01a064d3: the header used to be fed props.turnRows - the TRANSCRIPT's
    own rows, which SA_toTranscript deliberately never populates with
    llm_call records (session-adapter.jsx's SA_SKIP_IN_TRANSCRIPT) - so
    it silently undercounted against the trace BODY, which renders the
    turn's full timeline node tree (tool_call AND llm_call) fetched
    separately via SH_api.timeline. The header must aggregate over that
    SAME node list the body renders, not a narrower transcript-derived
    one - so both NV_TraceSplit's sidebar header and NV_TraceMaximize's
    overlay header now read the flattened timeline nodes (traceNodes),
    and the now-dead turnRowsFor/turnRows plumbing is gone, not just
    unused."""
    trace = DOC[DOC.index("function NV_TraceSplit"):]
    trace = trace[:trace.index("function NV_Composer")]
    assert "var traceNodes = flatRows.map(function (r) { return r.node; });" in trace
    assert "SH_traceHeaderLabel(props.turnNo, traceNodes)" in trace
    assert "SH_traceHeaderLabel(props.turnNo, props.traceNodes)" in trace
    assert "trace · turn {props.turnNo}" not in trace

    assert "function turnRowsFor(turnNo)" not in DOC
    assert "turnRows={turnRowsFor(traceTurn)}" not in DOC
    assert "props.turnRows" not in DOC


def test_divider_renders_as_a_rule_not_a_bubble_via_mini_racer():
    """Audit A item 1 (the worst gap): session-adapter.jsx already maps
    compaction_marker/rewind_marker/invocation_divider to kind "divider"
    with a computed label, but renderTurn had NO case for it before this
    fix - it fell through to the generic agent bubble, an empty identity
    chip dressed as something the agent said. The divider branch only
    needs `row` and SH_shortTime (no other closure state from
    NV_SessionDoc), so it is sliced out and transpiled alone - real JSX
    execution via the same JSXBundler primer/api/_jsx_bundle.py uses to
    serve the bundle, not a text-only check - against a React stub that
    captures the element tree instead of a real DOM (same "stub React,
    call the real logic" style as test_provider_form.py's
    PC_submittable harness), so this also catches a REORDERING regression
    (moving the divider branch after the generic return would make it
    unreachable) that source-grepping alone would miss.
    """
    import json

    from py_mini_racer import MiniRacer

    from primer.api._jsx_bundle import JSXBundler

    bundler = JSXBundler(
        ui_dir=UI, babel_source=(UI / "vendor" / "babel.min.js").read_text(encoding="utf-8"),
    )
    start = DOC.index('if (row.kind === "divider") {')
    end = DOC.index("\n    }\n", start) + len("\n    }\n")
    branch_src = DOC[start:end]
    wrapped = "function DIVIDER_BRANCH(row) {\n" + branch_src + "\n  return { type: \"NOT_A_DIVIDER\" };\n}\n"
    transpiled = bundler._transform(wrapped, "divider_branch_test.jsx")

    ctx = MiniRacer()
    ctx.eval(
        """
        var React = { createElement: function (type, props) {
          var children = Array.prototype.slice.call(arguments, 2);
          return { type: type, props: props || {}, children: children };
        }};
        function SH_shortTime(createdAt) { return createdAt ? "2:05 PM" : ""; }
        """
    )
    ctx.eval(transpiled)

    def render(row):
        ctx.eval("var __row = " + json.dumps(row) + ";")
        ctx.eval("var __out = DIVIDER_BRANCH(__row);")
        return json.loads(ctx.eval("JSON.stringify(__out)"))

    out = render({"kind": "divider", "seq": 9,
                   "label": "compacted 3 turns into a summary",
                   "createdAt": "2026-08-29T14:05:00Z"})
    assert out["type"] == "div"
    assert out["props"]["className"] == "nv-turn-divider"
    tree = json.dumps(out)
    assert "nv-turn nv-turn-agent" not in tree, (
        "a divider must never render the generic agent bubble"
    )
    assert "nv-turn-byline" not in tree, "a divider has no identity byline"
    assert "compacted 3 turns into a summary" in tree
    assert "2:05 PM" in tree

    # A non-divider row must fall through untouched (the sentinel proves
    # the branch's own `if` actually gates it).
    other = render({"kind": "user_message", "seq": 1, "label": "hi"})
    assert other["type"] == "NOT_A_DIVIDER"


def test_compact_is_wired_not_gated():
    # US-008 R3 item 4: the overflow menu row calls the real endpoint -
    # the S1 P2 "needs the endpoint" gate from earlier rounds is gone.
    # Scoped to NV_SessionHeader specifically (item 2 registered a
    # palette "Compact Session" verb elsewhere in this file, so an
    # unscoped search would land on the wrong "Compact" occurrence).
    header = DOC[DOC.index("function NV_SessionHeader"):
                 DOC.index("function NV_Thought")]
    compact = re.search(r"Compact[\s\S]{0,200}", header)
    assert compact and "disabled" not in compact.group(0)
    assert "props.onCompact" in header
    # Calls the real backend, not a stub.
    assert "SH_api.compact(wid, sid)" in DOC


def test_rewind_moved_from_the_menu_to_a_per_message_icon():
    """01a052a5 item 4: "Rewind..." no longer lives in NV_SessionHeader's
    overflow menu, nor behind a separate palette verb / multi-candidate
    picker (dead affordances per the user's dogfood - both just
    reopened a list to re-pick a target already visible on screen). It
    is now the ONLY rewind entry point: an icon beside each eligible
    user message in renderTurn, calling NV_doRewind directly with that
    row's own seq."""
    header = DOC[DOC.index("function NV_SessionHeader"):
                 DOC.index("function NV_Thought")]
    assert "onOpenRewind" not in header
    assert 'data-verb="session.rewind"' not in header
    assert ">Rewind" not in header

    user_branch = DOC[DOC.index('if (row.kind === "user_message")'):
                       DOC.index("nv-turn-text", DOC.index('if (row.kind === "user_message")'))]
    assert "rewindableSeqs[row.seq]" in user_branch
    assert 'data-testid={"nv-rewind-here:" + row.seq}' in user_branch
    assert "confirmDialog(" in user_branch
    assert "NV_doRewind(con.wid, sid, row.seq, refetchAll, con.toast)" in user_branch

    # The old multi-candidate picker and its palette verb are gone
    # entirely, not just unreached - a palette-only verb (no rendered
    # pointer affordance) is exactly what the dual-render guard
    # (test_shell_dual_render_guard.py) prohibits.
    assert "function NV_RewindPicker" not in DOC
    assert 'id: "session.rewind"' not in DOC
    assert "SH_api.rewind(wid, sid, toSeq)" in DOC, (
        "NV_doRewind (still the one function calling the endpoint) must survive"
    )


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
    end = DOC.index("\n// ---", start)
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


# ---------------------------------------------------------------------------
# uiv2 Wave 3 - resolved decision cards (Phase-2c missing-feature: gate
# cards used to be built with records:[] hardcoded, so an approved/
# rejected call vanished from the transcript instead of leaving the
# notes §2.4 resolved line). Derivation-logic correctness itself
# (timeout/cancelled classification, decided-by phrasing) is already
# covered by tests/ui/test_approvals_audit_derivation.py's 13 MiniRacer
# tests against the exact same nv-platform.jsx helpers this reuses -
# these pin the WIRING: real records reach the card, the right
# component renders, and it calls into that shared logic rather than
# re-deriving its own.
# ---------------------------------------------------------------------------


def test_records_are_no_longer_hardcoded_empty():
    assert "records: []" not in DOC
    assert "resolvedRecords.data" in DOC


def test_a_decide_refetches_resolved_records_not_just_gates():
    # Without this, an approve/reject fires the "resumed" tap event,
    # the pending card clears via gates.refetch(), and the new resolved
    # record silently never appears until the operator reloads.
    m = re.search(r"useWorkspaceTapListener\([\s\S]{0,600}", DOC)
    assert m and "gates.refetch()" in m.group(0)
    assert "resolvedRecords.refetch()" in m.group(0)


def test_session_scoped_records_resource_is_wired():
    assert "SH_api.sessionApprovalRecords(sid" in DOC
    assert "SH_api.keys.sessionRecords(sid)" in DOC


def test_resolved_items_render_the_dedicated_card_not_the_actionable_ones():
    m = re.search(r"gateItems\.map\(function \(item\) \{[\s\S]{0,1000}", DOC)
    assert m
    body = m.group(0)
    assert "if (item.resolved)" in body
    assert "<NV_ResolvedDecisionCard" in body
    # The branch must come BEFORE the pending approval/ask ternary, or a
    # resolved item could still fall through to the live-action cards.
    assert body.index("if (item.resolved)") < body.index("item.kind ===")


def test_resolved_decision_card_reuses_the_shared_derivation_not_its_own():
    card = DOC[DOC.index("function NV_ResolvedDecisionCard"):
               DOC.index("function NV_AskCard")]
    assert "NV_approvalDerivedStatus(rec)" in card
    assert "NV_approvalStatusColor(status)" in card
    assert "NV_approvalDecidedBy(rec, status)" in card
    assert "NV_approvalAt(item.decidedAt)" in card
    # No actions - a resolved record is already terminal (per Dev-
    # Backend's own read when we agreed the nv-session-doc.jsx split).
    assert "onClick" not in card
    assert 'data-testid="nv-decision-resolved-line"' in card


def test_resolved_decision_card_registers_no_new_css_debt():
    assert ".nv-dot-resolved" in STYLES


def test_shell_attention_records_carry_explicit_fields_for_the_card():
    # Not just `preview` (which the OLD, never-fed stub overloaded to
    # hold the reason text) - the card needs decision/decidedBy/reason/
    # requestedAt/decidedAt as their own fields to feed
    # NV_approvalDerivedStatus's real ToolApprovalRecord-shaped input.
    m = re.search(r"for \(var j = 0; j < records\.length[\s\S]{0,1400}", ATTENTION)
    assert m
    body = m.group(0)
    for field in ("decision: rec.decision", "decidedBy: rec.decided_by",
                  "reason: rec.reason", "requestedAt: rec.requested_at",
                  "decidedAt: rec.decided_at"):
        assert field in body, field


def test_records_endpoint_gained_a_session_filter_not_a_client_side_one():
    # The per-session transcript fetch scopes server-side
    # (session_id=...) rather than fetching a page of the whole
    # instance's records and filtering in JS on every open session.
    api = (ROOT / "primer" / "api" / "routers" / "tool_approval.py").read_text(
        encoding="utf-8",
    )
    assert "session_id" in api
    assert 'query = query.where("session_id", session_id)' in api
