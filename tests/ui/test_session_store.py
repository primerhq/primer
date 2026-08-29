"""Session conversation store (Phase 2): merge rules, channels, snapshot identity.

The store is a framework-free module (ui/foundation/session-store.js) loaded
here exactly like test_shell_turns.py loads shell-turns.js: a MiniRacer
context with `var window = globalThis;`, no React. We drive the merge through
the window.* API and inspect the committed transcript snapshot.

Covers the spec's merge table (section 2.5) and the snapshot-identity rule
(section 3.3): delta accumulate, durable-replace (A4), optimistic reconcile,
and a stable getSnapshot across a no-op frame.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ui" / "foundation" / "session-store.js"


def _ctx() -> "py_mini_racer.MiniRacer":
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval("var window = globalThis;")
    # Force the no-scheduler path so a transcript commit happens synchronously
    # (the min-commit gate would otherwise defer to rAF/timeout, neither of
    # which a bare MiniRacer context provides).
    ctx.eval("window.setTimeout = undefined; window.requestAnimationFrame = undefined;")
    ctx.eval(MODULE.read_text(encoding="utf-8"))
    return ctx


def _transcript(ctx, wid, sid, frames):
    """Apply `frames` to a fresh store and return the committed transcript
    rows as a list of dicts."""
    out = ctx.eval(
        "(function () {\n"
        "  var s = SS_getStore(" + json.dumps(wid) + ", " + json.dumps(sid) + ");\n"
        "  var frames = " + json.dumps(frames) + ";\n"
        "  for (var i = 0; i < frames.length; i++) SS_apply(s, frames[i]);\n"
        "  return JSON.stringify(SS_getSnapshot(s, 'transcript') || []);\n"
        "})()"
    )
    return json.loads(out)


def test_deltas_accumulate_into_a_live_part() -> None:
    ctx = _ctx()
    rows = _transcript(ctx, "w1", "s1", [
        {"class": "part_start", "session_id": "s1", "part_id": "n1:text", "kind": "text"},
        {"class": "text_delta", "session_id": "s1", "part_id": "n1:text", "delta": "Hel"},
        {"class": "text_delta", "session_id": "s1", "part_id": "n1:text", "delta": "lo"},
        {"class": "text_delta", "session_id": "s1", "part_id": "n1:text", "delta": "!"},
    ])
    parts = [r for r in rows if r["kind"] == "part"]
    assert len(parts) == 1
    assert parts[0]["partId"] == "n1:text"
    assert parts[0]["text"] == "Hello!"
    assert parts[0]["state"] == "streaming"


def test_reasoning_delta_uses_the_reasoning_part_id() -> None:
    ctx = _ctx()
    rows = _transcript(ctx, "w1", "s2", [
        {"class": "part_start", "session_id": "s2", "part_id": "n1:reasoning", "kind": "reasoning"},
        {"class": "reasoning_delta", "session_id": "s2", "part_id": "n1:reasoning", "delta": "let me"},
        {"class": "reasoning_delta", "session_id": "s2", "part_id": "n1:reasoning", "delta": " think"},
    ])
    parts = [r for r in rows if r["kind"] == "part"]
    assert parts[0]["partKind"] == "reasoning"
    assert parts[0]["text"] == "let me think"


def test_durable_record_replaces_the_live_part() -> None:
    """A4: a durable record with the same part_id REPLACES the accumulated
    live part; a later delta for it is a no-op and the snapshot is unchanged."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SS_getStore("w2", "s3");
          SS_apply(s, {"class": "part_start", session_id: "s3",
            part_id: "n1:text", kind: "text"});
          SS_apply(s, {"class": "text_delta", session_id: "s3",
            part_id: "n1:text", delta: "partial"});
          var before = SS_getSnapshot(s, "transcript");
          SS_apply(s, {"class": "assistant_token", session_id: "s3", seq: 10,
            payload: {part_id: "n1:text", text: "final answer"}});
          var afterDurable = SS_getSnapshot(s, "transcript");
          var liveParts = afterDurable.filter(function (r) {
            return r.kind === "part" && r.partId === "n1:text"; });
          var rec = afterDurable.filter(function (r) {
            return r.kind === "record" && r.partId === "n1:text"; })[0];
          // a late delta for the finalized part must be a no-op
          SS_apply(s, {"class": "text_delta", session_id: "s3",
            part_id: "n1:text", "delta": "MORE"});
          var afterLate = SS_getSnapshot(s, "transcript");
          return JSON.stringify({
            liveParts: liveParts.length,
            recordText: rec ? rec.text : null,
            recordKind: rec ? rec.recordKind : null,
            unchangedAfterLateDelta: afterLate === afterDurable,
            changedByDurable: afterDurable !== before
          });
        })()
        """
    ))
    assert out["liveParts"] == 0          # superseded by the durable record
    assert out["recordText"] == "final answer"
    assert out["recordKind"] == "assistant_token"
    assert out["unchangedAfterLateDelta"] is True
    assert out["changedByDurable"] is True


def test_optimistic_row_reconciles_with_the_server_record() -> None:
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          window.primerApi = { apiFetch: function () {
            return { then: function (cb) { if (cb) cb(); return this; },
                     catch: function () { return this; } };
          }};
          var s = SS_getStore("w4", "s4");
          SS_sendUserMessage(s, "hi there", "c1");
          var before = SS_getSnapshot(s, "transcript");
          var opt = before.filter(function (r) { return r.kind === "optimistic"; });
          var statusBefore = SS_getSnapshot(s, "status");
          // the server's user_input record reconciles the optimistic row
          SS_apply(s, {"class": "user_input", session_id: "s4", seq: 20,
            payload: {text: "hi there"}});
          var after = SS_getSnapshot(s, "transcript");
          var opt2 = after.filter(function (r) { return r.kind === "optimistic"; });
          var rec = after.filter(function (r) {
            return r.kind === "record" && r.recordKind === "user_input"; });
          return JSON.stringify({
            optBefore: opt.length,
            optBeforeText: opt[0] ? opt[0].text : null,
            sendingStatus: statusBefore ? statusBefore.verb : null,
            optAfter: opt2.length,
            recordAfter: rec.length,
            recordText: rec[0] ? rec[0].text : null
          });
        })()
        """
    ))
    assert out["optBefore"] == 1
    assert out["optBeforeText"] == "hi there"
    assert out["sendingStatus"] == "sending"
    assert out["optAfter"] == 0            # reconciled (removed)
    assert out["recordAfter"] == 1        # the durable record stands in
    assert out["recordText"] == "hi there"


def test_snapshot_is_stable_across_a_no_op_frame() -> None:
    """getSnapshot returns a stable reference across a no-op frame; a real
    change (connState) reassigns the gates snapshot."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SS_getStore("w5", "s5");
          SS_apply(s, {"class": "part_start", session_id: "s5",
            part_id: "n1:text", kind: "text"});
          SS_apply(s, {"class": "text_delta", session_id: "s5",
            part_id: "n1:text", delta: "abc"});
          var a = SS_getSnapshot(s, "transcript");
          // a no-op frame (part_start for an existing part) must not
          // reassign the committed transcript snapshot
          SS_apply(s, {"class": "part_start", session_id: "s5",
            part_id: "n1:text", kind: "text"});
          var b = SS_getSnapshot(s, "transcript");
          var g0 = SS_getSnapshot(s, "gates");
          SS_setConnState(s, "error");
          var g1 = SS_getSnapshot(s, "gates");
          return JSON.stringify({
            aIsNull: a === null,
            transcriptStable: a === b,
            gatesChanged: g0 !== g1,
            gatesConnState: g1 ? g1.connState : null,
            gatesDegraded: g1 ? g1.degraded : null
          });
        })()
        """
    ))
    assert out["aIsNull"] is False
    assert out["transcriptStable"] is True
    assert out["gatesChanged"] is True
    assert out["gatesConnState"] == "error"
    assert out["gatesDegraded"] is True


def test_refcount_does_not_clear_accumulated_state() -> None:
    """dispose() drops the refcount to zero but keeps the accumulated
    records (background sessions keep accumulating)."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SS_getStore("w6", "s6");
          SS_retain(s);
          SS_apply(s, {"class": "assistant_token", session_id: "s6", seq: 1,
            payload: {text: "kept"}});
          SS_dispose(s);
          var refs = s.refs;
          var snap = SS_getSnapshot(s, "transcript");
          var rec = snap.filter(function (r) { return r.kind === "record"; });
          return JSON.stringify({
            refs: refs,
            recordKept: rec.length === 1 && rec[0].text === "kept"
          });
        })()
        """
    ))
    assert out["refs"] == 0
    assert out["recordKept"] is True


def test_frames_for_other_sessions_do_not_touch_the_store() -> None:
    """A frame naming a different session is dropped (the store is per-sid)."""
    ctx = _ctx()
    rows = _transcript(ctx, "w7", "s7", [
        {"class": "text_delta", "session_id": "other", "part_id": "n1:text", "delta": "nope"},
        {"class": "assistant_token", "session_id": "other", "seq": 99, "payload": {"text": "nope"}},
    ])
    assert rows == []


def test_tap_durable_frame_normalizes_class_to_kind() -> None:
    """A tap durable frame names its kind field `class`; the store must
    record it with kind set from class so SA_toTranscript (which reads
    rec.kind) does not map it to a blank lifecycle row - without mutating
    the original (the hub shares it with the sidebar/status subscribers)."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SS_getStore("w9", "s9");
          var frame = {"class": "assistant_token", session_id: "s9",
            seq: 10, payload: {text: "hi"}};
          SS_apply(s, frame);
          var rec = s.records[10];
          return JSON.stringify({
            storedKind: rec ? rec.kind : null,
            storedClass: rec ? rec["class"] : null,
            storedIsClone: rec !== frame,
            originalHasKind: ("kind" in frame)
          });
        })()
        """
    ))
    assert out["storedKind"] == "assistant_token"
    assert out["storedClass"] == "assistant_token"   # the clone keeps class
    assert out["storedIsClone"] is True              # a new object was stored
    assert out["originalHasKind"] is False           # the original was not mutated


def test_record_already_carrying_kind_is_stored_as_is() -> None:
    """A record that already carries kind is stored as-is (no clone)."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SS_getStore("w10", "s10");
          var frame = {kind: "assistant_token", session_id: "s10",
            seq: 5, payload: {text: "hi"}};
          SS_apply(s, frame);
          var rec = s.records[5];
          return JSON.stringify({
            storedIsSame: rec === frame,
            kind: rec ? rec.kind : null
          });
        })()
        """
    ))
    assert out["storedIsSame"] is True
    assert out["kind"] == "assistant_token"


def test_tool_result_metadata_survives_the_class_to_kind_clone() -> None:
    """UX reconcile wave 7 (audit A items 4/6, render half): a live
    tool_result tap frame (shaped like ui/fixtures/shell/tap-frames.json's
    c2 grep frame) carries payload.metadata (match_count/file_count,
    wave 5's server-computed data) - the store/pipeline must not be a
    fourth drop point after the three the backend already fixed. The
    class-to-kind clone (SS_insertRecord) does `Object.assign({}, rec,
    {kind: ...})`, a shallow copy that carries payload (and therefore
    payload.metadata) by reference; this pins that down for real rather
    than by reading the source."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SS_getStore("ws-3f8a9bc1d4e2", "sess-4f1a2b3c");
          var frame = {
            cursor: "c2", seq: 43,
            workspace_id: "ws-3f8a9bc1d4e2", session_id: "sess-4f1a2b3c",
            agent_id: "operator", graph_id: null, node_id: null,
            "class": "tool_result", ts: "2026-08-16T09:40:05+00:00",
            payload: {
              tool_call_id: "tc-1",
              content: "src/api.ts:88\\nsrc/api.ts:104",
              metadata: {match_count: 2, file_count: 1, truncated: false}
            }
          };
          SS_apply(s, frame);
          var rec = s.records[43];
          return JSON.stringify({
            kind: rec ? rec.kind : null,
            metadata: rec ? rec.payload.metadata : null
          });
        })()
        """
    ))
    assert out["kind"] == "tool_result"
    assert out["metadata"] == {
        "match_count": 2, "file_count": 1, "truncated": False,
    }


def test_legacy_role_parts_frame_renders_as_a_legacy_row() -> None:
    """SEV-2: refresh mid-turn returned a `{role, parts}` legacy Message row
    (primer/workspace/session.py's append_instruction, written for the
    user's own first instruction well before its SessionMessageRecord
    counterpart exists - primer/session/enqueue.py's dual-write) with no
    numeric seq at all. SS_apply used to drop it silently: not a delta frame
    (no class), not a durable record (no seq) - leaving the pane blank for
    the whole gap. This pins the fix: the placeholder is recognized and
    surfaced as a `kind: "legacy"` row, ahead of the (empty, here) real
    record list - see the next test for the supersede half."""
    ctx = _ctx()
    rows = _transcript(ctx, "w7", "s7", [
        {"role": "user", "parts": [{"type": "text", "text": "hello world"}],
         "session_id": "s7"},
    ])
    legacy = [r for r in rows if r["kind"] == "legacy"]
    assert len(legacy) == 1
    assert legacy[0]["role"] == "user"
    assert legacy[0]["text"] == "hello world"


def test_legacy_message_is_superseded_by_a_rest_shaped_real_record() -> None:
    """The real record can arrive REST-shaped (`kind`, not `class` - the
    same shape get_session_messages returns, per SS_insertRecord's own
    documented convention) rather than as a live tap frame. Pins two
    things at once: recordKind's `kind || frame.kind` fallback (a REST
    user_input used to be invisible to this check, `class` alone), and
    that a real record dedupes out the placeholder it supersedes rather
    than rendering both (the user's own message showing up twice)."""
    ctx = _ctx()
    rows = _transcript(ctx, "w8", "s8", [
        {"role": "user", "parts": [{"type": "text", "text": "hello world"}],
         "session_id": "s8"},
    ])
    assert len([r for r in rows if r["kind"] == "legacy"]) == 1

    rows = _transcript(ctx, "w8", "s8", [
        {"kind": "user_input", "session_id": "s8", "seq": 5,
         "payload": {"text": "hello world"}},
    ])
    legacy = [r for r in rows if r["kind"] == "legacy"]
    records = [r for r in rows if r["kind"] == "record"]
    assert legacy == []
    assert len(records) == 1
    assert records[0]["text"] == "hello world"


def test_legacy_message_is_deduped_against_a_repeat_of_itself() -> None:
    """messages.jsonl can carry the same instruction's legacy line twice
    (e.g. session create with initial_instructions plus a later steer
    sharing the file) - the placeholder must not render twice."""
    ctx = _ctx()
    frame = {"role": "user", "parts": [{"type": "text", "text": "hi"}],
             "session_id": "s9"}
    rows = _transcript(ctx, "w9", "s9", [frame, frame])
    assert len([r for r in rows if r["kind"] == "legacy"]) == 1


def test_legacy_message_is_skipped_when_a_real_record_already_exists() -> None:
    """Frame order is not guaranteed (a REST backfill page and a live tap
    catch-up can race) - if the real record is already present, a legacy
    line for the same text must never be added at all, not added-then-
    removed."""
    ctx = _ctx()
    rows = _transcript(ctx, "w10", "s10", [
        {"class": "user_input", "session_id": "s10", "seq": 1,
         "payload": {"text": "hi"}},
        {"role": "user", "parts": [{"type": "text", "text": "hi"}],
         "session_id": "s10"},
    ])
    assert len([r for r in rows if r["kind"] == "legacy"]) == 0
    assert len([r for r in rows if r["kind"] == "record"]) == 1


def test_legacy_placeholder_renders_ahead_of_a_later_unrelated_record() -> None:
    """Full reconnect sequence the diagnosis captured: mount with an empty
    store, backfill delivers the legacy placeholder for the first message
    AND an unrelated durable record (e.g. a later turn's assistant
    answer) in the same burst - the placeholder must render as the
    FIRST row, not be lost among or after the real records."""
    ctx = _ctx()
    rows = _transcript(ctx, "w11", "s11", [
        {"role": "user", "parts": [{"type": "text", "text": "first message"}],
         "session_id": "s11"},
        {"class": "assistant_token", "session_id": "s11", "seq": 7,
         "payload": {"text": "an answer"}},
    ])
    assert rows[0]["kind"] == "legacy"
    assert rows[0]["text"] == "first message"
    assert rows[1]["kind"] == "record"
    assert rows[1]["text"] == "an answer"


def test_status_updates_from_a_rest_shaped_record_too() -> None:
    """SS_updateStatus had the identical frame["class"]-only blind spot as
    SS_apply's old recordKind bug: a REST-seeded (kind, not class)
    user_input never set the live "thinking" status. Folded into this
    ticket per the lead's call (same shape, same file)."""
    ctx = _ctx()
    out = json.loads(ctx.eval(
        """
        (function () {
          var s = SS_getStore("w12", "s12");
          SS_apply(s, {"kind": "user_input", session_id: "s12", seq: 1,
            payload: {text: "hi"}});
          return JSON.stringify(SS_getSnapshot(s, "status"));
        })()
        """
    ))
    assert out["verb"] == "thinking"
