/* global React */
// Session adapter (Task 11, studio-agents-interact) — maps a workspace
// Session's message stream onto the shape chat-refactor's `<Transcript>`
// already knows how to render, so a Session can be rendered through the
// reused chat UI without a second parallel renderer.
//
// SA_ = Session Adapter. No-build scope rule: top-level `var`/`function`
// declarations (mirrors ui/components/shared/transcript.jsx's own top-level
// style, not the IIFE-wrapped helper style of use-transcript.js) with
// every exported symbol assigned to `window.X` at file end.
//
// Transport note: the live data hook (REST history seed + tap SSE +
// catch-up) moved to ui/foundation/session-store.js and
// ui/foundation/use-workspace-tap.js in Phase 2. This module now holds
// only the pure record->transcript mapping (SA_toTranscript + the kind
// tables) so a consumer can render the store's records through the
// reused chat UI without a second parallel renderer.
//
// Two symbols are produced:
//   - SA_toTranscript(records, session): pure mapping, SessionMessageKind
//     (or the tap's mirrored TapEventClass, once normalised to the same
//     {seq, kind, payload, created_at, node_id} shape) -> the transcript
//     row shape.
//   - SA_visibleRecords(records): the progressive rewind-fold walk
//     SA_toTranscript runs first, ported from primer/session/replay.py's
//     visible_records (exported separately so nested-rewind composition
//     can be tested against the raw record set, not just the mapped
//     transcript rows).

// ---------------------------------------------------------------------------
// Pure mapping: SessionMessageKind -> transcript row kind
// ---------------------------------------------------------------------------

// Kinds that are structural instructions rather than content, and so
// never reach the reader. ONE table by design: S3 adds client_action
// and S7 adds llm_call HERE rather than each introducing its own
// registry at this same insertion point (cross-plan findings F26/F36).
// S8 HAND-OFF: SH_TurnList replaces this renderer at the flag day, and
// the S8 plan names none of the four kinds P1 added. Carry these three
// decisions across or they regress: reasoning collapses muted,
// agent_marker is a binding row, external_tool_call folds into the tool
// rendering.
//
// US-008 R3 item 4: rewind_marker moved OUT of this table and into
// SA_KIND_TO_TRANSCRIPT as a divider (below) - now that the console
// actually wires Rewind, skipping it left the reader looking at a
// transcript a rewind visibly did nothing to (the /messages read is
// visible=false by design - see primer/api/routers/sessions.py's own
// comment - so the raw discarded rows were never hidden either).
// SA_toTranscript's fold pass now does what the replay walk does
// server-side: hide the span the marker discarded and label where it
// cut, same principle already applied to compaction_marker below.
var SA_SKIP_IN_TRANSCRIPT = {
  // Delivery frame for a notifying tool call (S3): display and protocol
  // only. The paired tool_call/tool_result rows carry the history, so
  // rendering this too would show the same action twice.
  client_action: true,
  // Per-model-call trace data (S7): the Trace panel reads it from the
  // timeline endpoint, and the paged /messages read still returns it.
  // Only this renderer skips it, so it cannot fall through to the
  // generic "lifecycle" bubble the mapper gives every unmapped kind.
  llm_call: true,
  // A compaction that could not reduce the prompt and wrote no marker
  // (primer/agent/compaction.py): session-record visibility for operators
  // and the paged /messages read; not conversation.
  compaction_note: true,
};

var SA_KIND_TO_TRANSCRIPT = {
  user_input: "user_message",
  assistant_token: "assistant_message",
  // Model thinking, shown collapsed and muted. Never an assistant
  // message: replaying reasoning back as an answer misreads what it is.
  reasoning: "reasoning",
  tool_call: "tool_call",
  tool_result: "tool_result",
  // An invoker-supplied tool call renders as a tool call. It differs in
  // WHO executes it, which the reader does not care about, so a third
  // row shape would be noise.
  external_tool_call: "tool_call",
  // Binding hand-off: which agent took over, and at which epoch.
  agent_marker: "binding_change",
  graph_transition: "divider",
  // The reader SHOULD see that their history was folded: after a
  // compaction the marker is the only visible row (replay.visible_records
  // replaces the set with it), so hiding it would leave a transcript that
  // silently begins mid-conversation.
  compaction_marker: "divider",
  invocation_divider: "divider",
  // Unlike compaction (annotates, keeps the raw span visible for
  // audit), a rewind's whole point is to discard - SA_toTranscript's
  // fold pass below hides the span it names before this label ever
  // renders.
  rewind_marker: "divider",
  // An operator's pause was touched by an incoming wake (01a08c08): a
  // visible divider, never a generic lifecycle dot, because the whole
  // point of the record is that the change is not silent.
  pause_superseded: "divider",
  // Lifecycle rows map to the SAME-named kinds <Transcript>'s Message()
  // already renders with dedicated styling: yielded/resumed/done as a muted
  // "· kind" dot, cancelled as a red "■ cancelled" marker, error as an error
  // banner. Collapsing them into a generic "lifecycle"/"interaction" bucket
  // (which Message() has no branch for) fell through to the plain agent
  // bubble and lost that styling.
  yielded: "yielded",
  resumed: "resumed",
  done: "done",
  cancelled: "cancelled",
  error: "error",
};

// UX reconcile wave 3 (audit A item 6): the live/done "editing {file}
// +N -N" chip's line-delta data for an edit result. workspace__edit_file's
// own tool result is a unified diff already (primer/workspace/local/
// tools/edit.py's difflib.unified_diff, returned verbatim as
// ToolResult.output) - this counts the +/- content lines, skipping the
// "+++"/"---" filename header pair a unified diff always opens with
// (those are not content changes). Generic over any unified-diff text,
// not edit-specific, so it is equally usable against a git patch string
// elsewhere. Internal to this file - a caller wanting the diff stat for
// an arbitrary tool_result row should use SA_diffStatOfResult below,
// which also covers write.
function SA_diffStatOf(text) {
  var lines = String(text || "").split("\n");
  var additions = 0;
  var deletions = 0;
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    if (line.indexOf("+++") === 0 || line.indexOf("---") === 0) continue;
    if (line.charAt(0) === "+") additions += 1;
    else if (line.charAt(0) === "-") deletions += 1;
  }
  if (!additions && !deletions) return null;
  return { additions: additions, deletions: deletions };
}

// UX reconcile wave 5 (audit A item 6, write half): the ONE seam a
// caller should use for a tool_result row's diff stat, regardless of
// which tool produced it. write's own metadata (server-computed via
// difflib.SequenceMatcher against the old file content, captured just
// before it was overwritten - primer/workspace/local/tools/write.py) is
// authoritative when present; otherwise falls back to parsing edit's
// unified-diff output text via SA_diffStatOf above. A row with neither
// (a tool that produces no diff, or a pre-wave-5 record with no
// metadata key at all) returns null - the same "no chip" contract
// SA_diffStatOf already has.
function SA_diffStatOfResult(rec) {
  var payload = (rec && rec.payload) || {};
  var meta = payload.metadata || null;
  if (
    meta
    && (typeof meta.additions === "number" || typeof meta.deletions === "number")
  ) {
    return { additions: meta.additions || 0, deletions: meta.deletions || 0 };
  }
  return SA_diffStatOf(payload.output);
}

// UX reconcile wave 5 (audit A item 4): the "searched N files" result-
// count label for a grep tool_result row, read from its own exact
// metadata (primer/workspace/local/tools/grep.py's match_count/
// file_count/truncated, restored server-side in wave 5) rather than a
// client-side parse of the (possibly head_limit-capped) output text -
// the wave 3 report's own argument against that parse still applies: a
// capped output list of "4" lines must never be presented as if it
// were the total when the true count is "250+". Copy choice (mine, not
// a reference mock - noted per the brief): "searched N file(s)", with a
// trailing "+" on the count when metadata.truncated is true. Returns
// null when metadata is absent (a mid-flight tool_result before the
// executor attaches it, or a pre-wave-5 persisted record) - the
// caller's existing chip keeps showing its input-arg form (e.g. the
// raw pattern) in that case, exactly as it does today.
function SA_resultCountLabel(rec) {
  var payload = (rec && rec.payload) || {};
  var meta = payload.metadata || null;
  if (!meta || typeof meta.file_count !== "number") return null;
  var count = String(meta.file_count) + (meta.truncated ? "+" : "");
  var noun = meta.file_count === 1 && !meta.truncated ? "file" : "files";
  return "searched " + count + " " + noun;
}

// The text a row shows. Messages keep theirs at payload.text; a record
// with none (a tool call, a lifecycle marker) has nothing to say here and
// the renderer draws its own chip for it.
function SA_rowText(rec) {
  var payload = rec.payload || {};
  if (typeof payload.text === "string" && payload.text) return payload.text;
  // Pending steers are stored as parts, so a realized one may arrive in
  // that shape too. Join the text parts, as the queue chip does.
  if (Array.isArray(payload.parts)) {
    var out = [];
    for (var i = 0; i < payload.parts.length; i++) {
      var part = payload.parts[i];
      if (part && part.type === "text" && part.text) out.push(part.text);
    }
    if (out.length) return out.join("\n");
  }
  return undefined;
}


// " (summariser input reduced: ...)" for a compaction marker whose summariser
// overflowed and was retried on less (summary_input_reduced: {pruned,
// folded_chunks, truncated_parts}), " (summariser tool loop cut in round N)" for
// a tool-enabled summariser whose loop overflowed in a later round
// (tool_loop_cut_round; the retry and the cut are exclusive), "" for an ordinary
// compaction.
function SA_reducedInputNote(r) {
  if (!r) return "";
  var parts = [];
  if (r.pruned > 0) {
    parts.push(r.pruned + (r.pruned === 1 ? " tool result" : " tool results") + " left out");
  }
  if (r.folded_chunks > 1) parts.push("read in " + r.folded_chunks + " chunks");
  if (r.truncated_parts > 0) {
    parts.push(r.truncated_parts + (r.truncated_parts === 1 ? " part" : " parts") + " cut");
  }
  // a tool-enabled summariser's loop overflowed in a later round and ended with the summary it had written
  // (no retry ran then, so no reduction counts come with it)
  if (r.tool_loop_cut_round > 0) return " (summariser tool loop cut in round " + r.tool_loop_cut_round + ")";
  if (parts.length) return " (summariser input reduced: " + parts.join(", ") + ")";
  // all zero: the retry was text only on an input that needed nothing taken out
  return " (summariser retried without tools)";
}

// Divider label for the four kinds SA_KIND_TO_TRANSCRIPT maps to "divider".
// invocation_divider (written by reset_session on ENDED->CREATED re-open,
// payload: {invocation: N}) renders "— invocation N —"; graph_transition
// (node ENTER/EXIT, payload: {node_id, node_kind, phase, status}) renders
// "<node_id> · <phase>".
function SA_dividerLabel(rec) {
  if (rec.kind === "compaction_marker") {
    var p = rec.payload || {};
    var from = p.replaced_from_seq;
    var base = from == null
      ? "\u2014 history compacted"
      : "\u2014 history compacted from #" + from;
    // The summariser's own call overflowed and it was retried on a smaller
    // input (primer/agent/summary_input.py): its summary is of less than the
    // whole span, which an ordinary compaction divider would not say.
    return base + SA_reducedInputNote(p.summary_input_reduced) + " \u2014";
  }
  if (rec.kind === "rewind_marker") {
    var rp = rec.payload || {};
    return "— rewound, later turns discarded (kept up to #"
      + rp.to_seq + ") —";
  }
  if (rec.kind === "invocation_divider") {
    var n = (rec.payload && rec.payload.invocation) || 1;
    return "— invocation " + n + " —";
  }
  if (rec.kind === "pause_superseded") {
    var sp = rec.payload || {};
    if (sp.action === "cleared") {
      return "— pause cleared by a new message —";
    }
    if (sp.action === "queued") {
      return "— session is paused: automated message queued, pause kept —";
    }
    if (sp.action === "dropped") {
      var gone = String(sp.text || "");
      if (gone.length > 80) gone = gone.slice(0, 80) + "…";
      return "— queued message dropped (" + (sp.reason || "queue full")
        + "): " + gone + " —";
    }
    return "— pause superseded —";
  }
  var p = rec.payload || {};
  return (p.node_id || "node") + " · " + (p.phase || "");
}

// The read path is visible=false (primer/api/routers/sessions.py's own
// comment: the console needs the raw stream for audit/trace), so a
// rewind's discarded rows arrive here same as anything else - nothing
// upstream hides them. This ports primer/session/replay.py's
// visible_records walk faithfully for the REWIND rule (its own
// docstring: "Rewind, continue, rewind again nests correctly" - acting
// on the running VISIBLE set rather than raw file order is what makes
// nested rewinds compose). `records` is seq-ascending
// (session-store.js's recordsBySeq contract), matching the walk's
// append-order assumption.
//
// Diverges from the backend in ONE place, by design: there, a
// rewind_marker is a pure instruction and is never returned (`continue`,
// never appended) - here it stays in the visible set, because the
// console needs to SHOW the reader a rewind happened (US-008 R3 item 4),
// not just silently honor it; SA_KIND_TO_TRANSCRIPT renders it as a
// divider below. compaction_marker is deliberately NOT ported the same
// way - the backend replaces the whole visible set with the marker
// (folds it into a summary); item 4's accepted design keeps the raw
// pre-compaction span visible for audit and only annotates, so it is
// still just appended here, never hides anything.
function SA_visibleRecords(records) {
  var visible = [];
  for (var i = 0; i < records.length; i++) {
    var rec = records[i];
    if (rec.kind === "rewind_marker") {
      var toSeq = (rec.payload || {}).to_seq;
      if (typeof toSeq === "number") {
        visible = visible.filter(function (r) { return r.seq <= toSeq; });
      }
      visible.push(rec);
      continue;
    }
    visible.push(rec);
  }
  return visible;
}

// records: SessionMessageRecord-shaped rows — {seq, kind, payload,
// created_at, node_id}, whether loaded from the REST history endpoint or
// normalised from a live TapEvent by the tap hub (Phase 2).
// session: the WorkspaceSession row (reserved for session-aware rendering
// decisions a future task may need — not read here yet).
// A failed stream writes the error WITH its cause and then a bare terminal marker ({reason, terminal}) for the same failure. The
// marker has nothing to say; rendered as its own row it was a second red "turn failed" card under the real one.
function SA_isBareTerminalError(rec) {
  var p = rec.payload || {};
  return rec.kind === "error" && p.terminal === true && !p.message && !p.code && !p.title;
}

// Whose failure an error row is: the PARENT turn's own (null), or one subagent run's. A subagent's records are drawn inside its own block
// and carry payload.delegated (plus delegate_run_id, or delegate_tool_call_id on records written before runs had ids). Folding across the two
// would make a turn whose subagent and parent hit the same provider error fail with no card of its own at the top level.
function SA_failureScope(rec) {
  var p = rec.payload || {};
  if (!p.delegated) return null;
  return String(p.delegate_run_id || p.delegate_tool_call_id || "delegated");
}

function SA_toTranscript(records, session) {
  var visible = SA_visibleRecords(records);
  var out = [];
  // One failure is one card, within a turn. A failed model call writes the stream's own Error row, then (once the loop raises)
  // dispatch writes its ERROR record with the same words, then a bare terminal marker; streamed text can sit between any two of
  // them. The first row that carries the cause is the one kept (it has the specific code); a later row with the same message from
  // the same node, or a bare marker, is a copy of it. A turn is whatever lies between two user messages.
  //
  // This leans on the order the writers use: the cause is written BEFORE its copies and its marker. A copy that came before its cause
  // would be kept and the cause folded into it (the first row with the words wins), and a marker that came first is removed when the
  // cause arrives (below). A row without a node id matches a cause of ANY node (records from before graphs named theirs carry none),
  // so two graph nodes failing with the same words stay two cards only when both rows name their node.
  //
  // Scope: parent and subagent failures never fold into each other (SA_failureScope), and a bare marker belongs to the scope that wrote
  // it. A non-fatal error ({fatal: false}, a retry notice) is an error row like any other: it is drawn, it can be the cause a later
  // fatal row with the same words folds into, and then the card carries fatal=false.
  var turnCauses = [];
  var turnMarkers = {};
  for (var i = 0; i < visible.length; i++) {
    var rec = visible[i];
    if (SA_SKIP_IN_TRANSCRIPT[rec.kind]) continue;
    if (rec.kind === "user_input") { turnCauses = []; turnMarkers = {}; }
    var bare = SA_isBareTerminalError(rec);
    var scope = SA_failureScope(rec);
    var scopeKey = scope === null ? "" : scope;
    if (rec.kind === "error") {
      var message = (rec.payload || {}).message || null;
      if (bare) {
        // A marker with a cause of its own scope anywhere in its turn is that cause's marker. A marker with none is the only evidence and stays.
        if (turnCauses.some(function (c) { return c.scope === scope; })) continue;
      } else {
        var node = rec.node_id || null;
        var copy = message && turnCauses.some(function (c) {
          return c.scope === scope && c.message === message && (!c.node || !node || c.node === node);
        });
        if (copy) continue;
        turnCauses.push({ message: message, node: node, scope: scope });
        // A marker of this scope that came first gives way to the cause that follows it.
        if (turnMarkers[scopeKey]) {
          var at = out.indexOf(turnMarkers[scopeKey]);
          if (at >= 0) out.splice(at, 1);
          delete turnMarkers[scopeKey];
        }
      }
    }
    // A DONE carrying stop_reason="tool_use" ends one MODEL CALL, not
    // the turn: the loop runs the tools and calls the model again
    // (primer/session/timeline.py closes_turn makes the same cut).
    // Rendering them peppered the transcript with "done" markers
    // between every tool round and made the fold split one turn into
    // many (live finding 2026-08-26).
    if (rec.kind === "done"
        && ((rec.payload || {}).stop_reason === "tool_use")) continue;
    var kind = SA_KIND_TO_TRANSCRIPT[rec.kind] || "lifecycle";
    var row = {
      seq: rec.seq,
      kind: kind,
      nodeId: rec.node_id || null,
      // What the row actually SAYS. Only dividers got a label, so every
      // message row rendered an empty body: a transcript of identity
      // chips with nothing beside them, for the operator's own messages
      // and the agent's answers alike. user_input and assistant_token
      // both carry their text at payload.text (primer/session/enqueue.py
      // and persistence.py), which is the one place it lives.
      label: kind === "divider"
        ? SA_dividerLabel(rec)
        : SA_rowText(rec),
      payload: rec.payload || {},
      createdAt: rec.created_at,
      bare: bare,
    };
    out.push(row);
    if (rec.kind === "error" && bare) turnMarkers[scopeKey] = row;
  }
  return out;
}

window.SA_diffStatOf = SA_diffStatOf;
window.SA_diffStatOfResult = SA_diffStatOfResult;
window.SA_resultCountLabel = SA_resultCountLabel;
window.SA_SKIP_IN_TRANSCRIPT = SA_SKIP_IN_TRANSCRIPT;
window.SA_toTranscript = SA_toTranscript;
window.SA_KIND_TO_TRANSCRIPT = SA_KIND_TO_TRANSCRIPT;
window.SA_visibleRecords = SA_visibleRecords;
