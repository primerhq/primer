/* global React */
// session-store.js - the per-(wid, sid) conversation store (Phase 2, plan
// 2.1-2.4). Framework-free core: a module-level registry of per-session
// store instances that survive component unmount (background sessions keep
// accumulating). React binds to it via useSyncExternalStore in the guarded
// hook at the bottom (the ONLY React code here).
//
// Spec: docs/superpowers/plans/2026-08-28-p2-client-store-spec.md.
//
// The store is the single source for a session's transcript (durable records
// + live delta parts + optimistic rows), its turn status, and its connection
// state. It is fed by the tap hub (use-workspace-tap.js routes session-scoped
// frames here) and by a REST seed / catch-up the store performs itself.
//
// Style matches shell-turns.js: top-level var declarations, SS_-prefixed
// helpers, every exported symbol written to window.X at the end.

// Minimum interval between transcript commits (the plan's ~80 ms min-commit;
// the AI SDK throttle analogue). A 120 Hz display must not commit faster than
// this even if requestAnimationFrame fires faster.
var SS_MIN_COMMIT_MS = 80;

// wid + ":" + sid -> store. Module-level so a store outlives the component
// that created it (background sessions keep accumulating).
var SS_STORES = {};
var SS_PREFIX = "session-store:";

function SS_key(wid, sid) {
  return SS_PREFIX + wid + ":" + sid;
}

// --- state model (spec section 2) -----------------------------------------
//   records / recordsBySeq: durable records keyed by seq (the seed + replay)
//   parts:                  live delta parts keyed by part_id
//   optimistic:             pending user rows keyed by client id
//   status:                 derived turn status (the 3-source merge, C4)
//   connState:              tap connection state (surfaced by the hub)
//   _snap / _dirty / _sub:  per-channel committed snapshot + subscriptions

function SS_getStore(wid, sid) {
  var key = SS_key(wid, sid);
  var store = SS_STORES[key];
  if (store) return store;
  store = {
    wid: wid,
    sid: sid,
    records: {},          // seq -> record
    recordsBySeq: [],     // sorted ascending by seq
    parts: {},           // part_id -> { kind, text, state, final }
    optimistic: {},       // clientId -> { clientId, text, state }
    status: null,         // { verb, object, startedMs } | null
    connState: (window.WTAP_hubConnState
      && window.WTAP_hubConnState(wid)) || "connecting",
    refs: 0,
    _snap: { transcript: null, status: null, gates: null },
    _dirty: { transcript: false, status: false, gates: false },
    _sub: { transcript: [], status: [], gates: [] },
    _lastTranscriptCommit: 0,
    _flushScheduled: false,
    _catchUpInFlight: false,
    _forceCommit: false,
  };
  SS_STORES[key] = store;
  return store;
}

// --- merge (spec section 2.5) --------------------------------------------

// Insert a durable record by seq (dedupe, keep sorted). Returns true if the
// record was new.
//
// Tap durable frames name their kind field `class`; REST history records
// name it `kind` (SA_toTranscript reads rec.kind, so a tap frame that
// arrives over the tap would map to a blank lifecycle row). When kind is
// missing but class is present, store a shallow clone with kind set from
// class - the original is shared with the sidebar/status subscribers and
// must not be mutated. Records that already carry kind are stored as-is.
function SS_insertRecord(store, rec) {
  var seq = rec && rec.seq;
  if (typeof seq !== "number") return false;
  if (store.records[seq] != null) return false;
  var stored = rec;
  if (rec.kind == null && rec["class"] != null) {
    stored = Object.assign({}, rec, { kind: rec["class"] });
  }
  store.records[seq] = stored;
  store.recordsBySeq.push(stored);
  store.recordsBySeq.sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); });
  return true;
}

// Reconcile a durable record that carries a part_id: it REPLACES the
// accumulated live part and marks it final (A4, p1-delta-stream-spec). The
// durable record is the authoritative final value; later deltas for the same
// part_id are no-ops.
function SS_finalizePart(store, partId, finalText) {
  var part = store.parts[partId];
  if (part) {
    part.final = true;
    part.state = "done";
    if (typeof finalText === "string" && finalText.length > 0) part.text = finalText;
    return true;
  }
  return false;
}

// Derive the turn status from the latest frame, mirroring the 3-source merge
// (correction C4): tap-derived live + optimistic "sending". The polled
// session row's turn_status is merged by the consumer (it is a separate REST
// source); the store owns the tap + optimistic legs.
function SS_updateStatus(store, frame) {
  var kind = frame && frame["class"];
  if (kind === "user_input") {
    store.status = { verb: "thinking", object: "", startedMs: SS_ms(frame) };
    return true;
  }
  if (kind === "tool_call") {
    var payload = frame.payload || {};
    store.status = {
      verb: String(payload.name || ""),
      object: "",
      startedMs: SS_ms(frame),
    };
    return true;
  }
  if (kind === "done" || kind === "cancelled" || kind === "error") {
    // A terminal record clears the live status, but an optimistic send that
    // reopened the session must not be wiped by a stale terminal frame: the
    // consumer layers the optimistic leg on top (C4). The store clears its
    // own live status; the optimistic row lives separately.
    if (!store.optimisticSendPending) {
      store.status = null;
      return true;
    }
  }
  return false;
}

function SS_ms(frame) {
  var raw = frame && frame.ts;
  if (raw) {
    var parsed = Date.parse(raw);
    if (!isNaN(parsed)) return parsed;
  }
  return Date.now();
}

// The merge entry point. `frame` is a parsed tap frame: either a durable
// record (has a numeric seq) or a delta frame (class is part_start /
// text_delta / reasoning_delta / tool_input_delta / part_end). The hub
// routes each frame here for the store whose sid matches frame.session_id.
function SS_apply(store, frame) {
  if (!frame || typeof frame !== "object") return;
  // The hub routes by session_id, but a frame that names another session is
  // dropped (defensive; the store is per-sid).
  if (frame.session_id != null && frame.session_id !== store.sid) return;

  var kind = frame["class"];
  var partId = frame.part_id;

  // Delta frames first (non-durable, no seq).
  if (kind === "part_start") {
    if (partId && !store.parts[partId]) {
      store.parts[partId] = {
        kind: frame.kind || "text",
        text: "",
        state: "streaming",
        final: false,
      };
    }
    SS_markDirty(store, "transcript");
    return;
  }
  if (kind === "text_delta" || kind === "reasoning_delta" || kind === "tool_input_delta") {
    var part = partId ? store.parts[partId] : null;
    // A delta for a missing part is a no-op (the durable record will still
    // arrive; A4). A part already finalized by its durable record ignores
    // later deltas (guards the interleave, A4).
    if (part && !part.final) {
      if (partId && !part.text) part.text = "";
      part.text = part.text + (typeof frame.delta === "string" ? frame.delta : "");
      part.state = "streaming";
      SS_markDirty(store, "transcript");
    }
    return;
  }
  if (kind === "part_end") {
    var ep = partId ? store.parts[partId] : null;
    if (ep && !ep.final) {
      ep.state = "done";   // an early caret hint; the durable record is the real finalize
      SS_markDirty(store, "transcript");
    }
    return;
  }

  // A durable record (has a numeric seq). Insert by seq, deduped + sorted.
  if (typeof frame.seq === "number") {
    var inserted = SS_insertRecord(store, frame);
    // Reconcile a part_id-bearing durable record: it replaces the live part
    // (A4). The durable record's part_id is in payload.part_id for
    // text/reasoning and is payload.id for a tool call.
    var reconcileId = (frame.payload && frame.payload.part_id)
      || (kind === "tool_call" && frame.payload && frame.payload.id)
      || null;
    if (reconcileId) {
      var finalText = (frame.payload && frame.payload.text) || null;
      SS_finalizePart(store, reconcileId, finalText);
    }
    // A user_input durable record reconciles the matching optimistic row.
    if (kind === "user_input" && store.optimisticSendPending != null) {
      SS_reconcileOptimistic(store);
    }
    SS_updateStatus(store, frame);
    SS_markDirty(store, "transcript");
    SS_markDirty(store, "status");
  }
}

// Reconcile a pending optimistic send with the server's user_input record:
// the optimistic row is removed; the durable record (now in recordsBySeq)
// stands in its place. The badge drops.
function SS_reconcileOptimistic(store) {
  if (store.optimisticSendPending != null) {
    var clientId = store.optimisticSendPending;
    if (store.optimistic[clientId]) delete store.optimistic[clientId];
    store.optimisticSendPending = null;
    SS_updateStatus(store, { "class": "user_input" });
  }
}

// --- channels + paint (spec section 3) -----------------------------------

// Mark a channel dirty. The transcript is batched (flush on rAF); status and
// gates commit immediately.
function SS_markDirty(store, channel) {
  if (channel === "transcript") {
    store._dirty.transcript = true;
    SS_scheduleFlush(store);
  } else {
    store._dirty[channel] = true;
    SS_commit(store, channel);
  }
}

// Commit a channel's snapshot. A new reference is produced ONLY when the
// content changed (the infinite-rerender guard, spec section 3.3).
function SS_commit(store, channel) {
  if (channel === "transcript") {
    var rows = SS_deriveTranscript(store);
    if (!SS_transcriptEqual(store._snap.transcript, rows)) {
      store._snap.transcript = rows;
    }
  } else if (channel === "status") {
    if (store._dirty.status) {
      store._snap.status = store.status;
    }
  } else if (channel === "gates") {
    if (store._dirty.gates) {
      store._snap.gates = {
        connState: store.connState,
        degraded: SS_isDegraded(store),
      };
    }
  }
  if (store._dirty[channel]) {
    store._dirty[channel] = false;
    SS_emitChannel(store, channel);
  }
}

// The derived transcript: a fold of durable records + live parts +
// optimistic rows. The exact row shape SA_toTranscript expects is the
// migration task's concern; here the fold produces a structural snapshot the
// consumer (and the test) can inspect. A live part that is final (its
// durable record has landed) is NOT shown - the record supersedes it (A4).
function SS_deriveTranscript(store) {
  var out = [];
  var i;
  for (i = 0; i < store.recordsBySeq.length; i++) {
    var rec = store.recordsBySeq[i];
    out.push({
      kind: "record",
      seq: rec.seq,
      recordKind: rec["class"],
      text: SS_recordText(rec),
      partId: (rec.payload && rec.payload.part_id) || null,
    });
  }
  // Live parts not yet finalized (their durable record has not landed).
  for (var pid in store.parts) {
    if (!Object.prototype.hasOwnProperty.call(store.parts, pid)) continue;
    var part = store.parts[pid];
    if (part.final) continue;   // superseded by its durable record (A4)
    out.push({
      kind: "part",
      partId: pid,
      partKind: part.kind,
      text: part.text,
      state: part.state,
    });
  }
  // Optimistic pending user rows.
  for (var cid in store.optimistic) {
    if (!Object.prototype.hasOwnProperty.call(store.optimistic, cid)) continue;
    var opt = store.optimistic[cid];
    out.push({
      kind: "optimistic",
      clientId: cid,
      text: opt.text,
      state: opt.state,
    });
  }
  return out;
}

function SS_recordText(rec) {
  var p = rec && rec.payload;
  if (p && typeof p.text === "string") return p.text;
  return "";
}

// Structural equality of two transcript snapshots (spec section 3.3): same
// length, same per-item kind / seq / partId / state, same text. A finished
// turn whose parts are all final and whose records are all durable produces
// a byte-identical snapshot across flushes, so no new reference is committed
// and the channel never re-fires.
function SS_transcriptEqual(a, b) {
  if (a === b) return true;
  if (a == null || b == null) return a == b;
  if (a.length !== b.length) return false;
  for (var i = 0; i < a.length; i++) {
    var x = a[i];
    var y = b[i];
    if (x.kind !== y.kind) return false;
    if (x.seq !== y.seq) return false;
    if (x.partId !== y.partId) return false;
    if (x.state !== y.state) return false;
    if (x.text !== y.text) return false;
  }
  return true;
}

// The rAF-batched transcript flush. The min-commit gate bounds how often a
// commit happens; the identity guard (in SS_commit) ensures a commit that does
// not change the row list does not reassign the snapshot.
function SS_flushTranscript(store) {
  var now = Date.now();
  if (!store._forceCommit
      && now - store._lastTranscriptCommit < SS_MIN_COMMIT_MS) {
    SS_scheduleFlush(store);
    return;
  }
  store._forceCommit = false;
  if (!store._dirty.transcript) return;
  store._lastTranscriptCommit = now;
  // Leave _dirty.transcript SET: SS_commit's tail owns the reset + emit
  // (clearing it here made the tail's emit dead code - the transcript
  // channel never notified; found via BDD session2 regressions).
  SS_commit(store, "transcript");
}

function SS_scheduleFlush(store) {
  if (store._flushScheduled) return;
  store._flushScheduled = true;
  var cb = function () {
    store._flushScheduled = false;
    SS_flushTranscript(store);
  };
  if (typeof window !== "undefined" && window.requestAnimationFrame) {
    window.requestAnimationFrame(cb);
  } else if (typeof setTimeout === "function") {
    setTimeout(cb, SS_MIN_COMMIT_MS);
  } else {
    // No scheduler: commit now, bypassing the min-commit gate (a bare env has
    // no clock to pace against, so the gate would otherwise defer forever).
    store._flushScheduled = false;
    store._forceCommit = true;
    SS_flushTranscript(store);
  }
}

function SS_emitChannel(store, channel) {
  var subs = store._sub[channel];
  if (!subs) return;
  var copy = subs.slice();
  for (var i = 0; i < copy.length; i++) {
    try { copy[i](); } catch (_e) { /* no-op */ }
  }
}

// --- snapshot identity (spec section 3.3) --------------------------------

// Returns the COMMITTED snapshot for a channel, verbatim. It never computes;
// it only returns store._snap[channel], which is reassigned only on a
// content-changing commit. This is what keeps useSyncExternalStore from
// re-rendering forever.
function SS_getSnapshot(store, channel) {
  return store._snap[channel];
}

function SS_subscribe(store, channel, fn) {
  if (typeof fn !== "function") return function () {};
  var subs = store._sub[channel];
  if (!subs) return function () {};
  subs.push(fn);
  return function () {
    var idx = subs.indexOf(fn);
    if (idx >= 0) subs.splice(idx, 1);
  };
}

// --- connection state + degradation (correction C5) ----------------------

// The single connState / degraded source the reconnect pill reads. The hub
// pushes connState here on open / error; the poll's degraded flag is merged
// by the caller that holds the useResource result (C5 keeps the store the
// single source for the tap leg).
function SS_setConnState(store, state) {
  if (store.connState === state) return;
  store.connState = state;
  SS_markDirty(store, "gates");
  SS_markDirty(store, "status");
}

function SS_isDegraded(store) {
  return store.connState === "error";
}

// --- optimistic send (plan 2.5) ------------------------------------------

// Append a local pending user row keyed by clientId BEFORE the POST. The
// server user_input record reconciles it (SS_apply / SS_reconcileOptimistic).
// Returns the fetch promise; on failure the caller restores the composer text
// and the optimistic row is removed here.
function SS_sendUserMessage(store, text, clientId) {
  store.optimistic[clientId] = {
    clientId: clientId,
    text: text,
    state: "pending",
  };
  store.optimisticSendPending = clientId;
  // The "sending" status leg (C4) shows immediately.
  store.status = { verb: "sending", object: "", startedMs: Date.now() };
  SS_markDirty(store, "transcript");
  SS_markDirty(store, "status");

  var wid = store.wid;
  var sid = store.sid;
  var apiFetch = (window.primerApi && window.primerApi.apiFetch) || null;
  if (typeof apiFetch !== "function") {
    return Promise.reject(new Error("SS_sendUserMessage: apiFetch unavailable"));
  }
  return apiFetch(
    "POST",
    "/workspaces/" + encodeURIComponent(wid) + "/sessions/" +
      encodeURIComponent(sid) + "/steer",
    { instruction: text }
  ).then(function () {
    // Success: the row stays pending until the server user_input record
    // arrives and reconciles it; the composer clears its text (Phase 0.2).
  }, function (err) {
    // Failure: remove the optimistic row; the caller restores the composer
    // text and shows an inline error row carrying err.requestId.
    delete store.optimistic[clientId];
    store.optimisticSendPending = null;
    SS_updateStatus(store, { "class": "user_input" });
    SS_markDirty(store, "transcript");
    SS_markDirty(store, "status");
    throw err;
  });
}

// --- refcount / dispose (spec section 4.4) -------------------------------

// refs==0 does NOT clear accumulated state (background sessions keep
// accumulating); it only stops the active flush + subscription work. The
// hub retains the store's connection; the store keeps its records.
function SS_retain(store) {
  store.refs += 1;
  return store;
}

function SS_dispose(store) {
  store.refs -= 1;
  if (store.refs <= 0) {
    store.refs = 0;
    // Stop the pending flush; keep the accumulated state.
    store._flushScheduled = false;
  }
  return store;
}

// --- cursor (moved from session-adapter.jsx SA_encodeCursor) ------------
// Build a base64url TapCursor token for one session's high-water seq. The
// hub opens the EventSource with this so the tap resumes from where the REST
// seed left off (the gap-free seam, p1-delta-stream-spec section 4).
function SS_encodeCursor(sid, seq) {
  var payload = { known_as_of: "1970-01-01T00:00:00+00:00", seqs: {} };
  payload.seqs[sid] = seq;
  var json = JSON.stringify(payload);
  var b64;
  try {
    b64 = btoa(unescape(encodeURIComponent(json)));
  } catch (_e) {
    return null;
  }
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Catch-up: the hub calls this on reconnect; the store re-reads the REST tail
// after its high-water and merges anything the tap missed (guarded so a
// reconnect + a poll tick cannot thrash the endpoint).
function SS_highWater(store) {
  if (store.recordsBySeq.length === 0) return 0;
  var last = store.recordsBySeq[store.recordsBySeq.length - 1];
  return (last && typeof last.seq === "number") ? last.seq : 0;
}

function SS_catchUp(store) {
  if (store._catchUpInFlight) return;
  var apiFetch = (window.primerApi && window.primerApi.apiFetch) || null;
  if (typeof apiFetch !== "function") return;
  store._catchUpInFlight = true;
  var afterSeq = SS_highWater(store);
  apiFetch(
    "GET",
    "/sessions/" + encodeURIComponent(store.sid) +
      "/messages?after_seq=" + afterSeq + "&limit=1000"
  ).then(function (res) {
    var items = (res && res.items) || [];
    var merged = 0;
    for (var i = 0; i < items.length; i++) {
      if (SS_insertRecord(store, items[i])) merged += 1;
    }
    if (merged > 0) {
      // A reconcile may also finalize a part or settle an optimistic row.
      for (var j = 0; j < items.length; j++) {
        SS_apply(store, items[j]);
      }
      SS_markDirty(store, "transcript");
      SS_markDirty(store, "status");
    }
  }).catch(function () {
    // best-effort; the next reconnect or poll retries.
  }).then(function () {
    store._catchUpInFlight = false;
  });
}

// --- React binding (the only React code; guarded so the core loads in a
// bare test env without React) ------------------------------------------
if (typeof window !== "undefined" && window.React &&
    typeof window.React.useSyncExternalStore === "function") {

  // Bind a component to one channel of one store. retain() on mount so the
  // store survives unmount; the underlying connection lives at the hub.
  window.useSessionStore = function (wid, sid, channel) {
    var store = SS_getStore(wid, sid);
    SS_retain(store);
    return window.React.useSyncExternalStore(
      function (cb) { return SS_subscribe(store, channel, cb); },
      function () { return SS_getSnapshot(store, channel); },
      function () { return SS_getSnapshot(store, channel); }
    );
  };
}

// --- window exports ------------------------------------------------------
window.SS_MIN_COMMIT_MS = SS_MIN_COMMIT_MS;
window.SS_getStore = SS_getStore;
window.SS_apply = SS_apply;
window.SS_getSnapshot = SS_getSnapshot;
window.SS_subscribe = SS_subscribe;
window.SS_sendUserMessage = SS_sendUserMessage;
window.SS_retain = SS_retain;
window.SS_dispose = SS_dispose;
window.SS_setConnState = SS_setConnState;
window.SS_catchUp = SS_catchUp;
window.SS_highWater = SS_highWater;
window.SS_encodeCursor = SS_encodeCursor;
// Exposed for the test harness (and for a future eviction policy): the
// module-level registry, and a force-flush for the transcript channel.
window.SS_STORES = SS_STORES;
window.SS_key = SS_key;
window.SS_flushTranscript = SS_flushTranscript;
