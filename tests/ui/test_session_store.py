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
        "  var s = SS_getStore(%s, %s);\n"
        "  var frames = %s;\n"
        "  for (var i = 0; i < frames.length; i++) SS_apply(s, frames[i]);\n"
        "  return JSON.stringify(SS_getSnapshot(s, 'transcript') || []);\n"
        "})()" % (json.dumps(wid), json.dumps(sid), json.dumps(frames))
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
