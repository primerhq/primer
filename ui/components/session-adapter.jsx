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
// One symbol is produced:
//   - SA_toTranscript(records, session): pure mapping, SessionMessageKind
//     (or the tap's mirrored TapEventClass, once normalised to the same
//     {seq, kind, payload, created_at, node_id} shape) -> the transcript
//     row shape.

// ---------------------------------------------------------------------------
// Pure mapping: SessionMessageKind -> transcript row kind
// ---------------------------------------------------------------------------

// Kinds that are structural instructions rather than content, and so
// never reach the reader. ONE table by design: S3 adds client_action
// and S7 adds llm_call HERE rather than each introducing its own
// registry at this same insertion point (cross-plan findings F26/F36).
// S8 HAND-OFF: SH_TurnList replaces this renderer at the flag day, and
// the S8 plan names none of the four kinds P1 added. Carry these four
// decisions across or they regress: reasoning collapses muted,
// agent_marker is a binding row, external_tool_call folds into the tool
// rendering, and rewind_marker is skipped entirely.

var SA_SKIP_IN_TRANSCRIPT = {
  // A rewind marker tells the replay walk what to drop. Rendering it
  // would show the reader an instruction about their history instead of
  // their history.
  rewind_marker: true,
  // Delivery frame for a notifying tool call (S3): display and protocol
  // only. The paired tool_call/tool_result rows carry the history, so
  // rendering this too would show the same action twice.
  client_action: true,
  // Per-model-call trace data (S7): the Trace panel reads it from the
  // timeline endpoint, and the paged /messages read still returns it.
  // Only this renderer skips it, so it cannot fall through to the
  // generic "lifecycle" bubble the mapper gives every unmapped kind.
  llm_call: true,
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


// Divider label for the three kinds SA_KIND_TO_TRANSCRIPT maps to "divider".
// invocation_divider (written by reset_session on ENDED->CREATED re-open,
// payload: {invocation: N}) renders "— invocation N —"; graph_transition
// (node ENTER/EXIT, payload: {node_id, node_kind, phase, status}) renders
// "<node_id> · <phase>".
function SA_dividerLabel(rec) {
  if (rec.kind === "compaction_marker") {
    var p = rec.payload || {};
    var from = p.replaced_from_seq;
    return from == null
      ? "— history compacted —"
      : "— history compacted from #" + from + " —";
  }
  if (rec.kind === "invocation_divider") {
    var n = (rec.payload && rec.payload.invocation) || 1;
    return "— invocation " + n + " —";
  }
  var p = rec.payload || {};
  return (p.node_id || "node") + " · " + (p.phase || "");
}

// records: SessionMessageRecord-shaped rows — {seq, kind, payload,
// created_at, node_id}, whether loaded from the REST history endpoint or
// normalised from a live TapEvent by the tap hub (Phase 2).
// session: the WorkspaceSession row (reserved for session-aware rendering
// decisions a future task may need — not read here yet).
function SA_toTranscript(records, session) {
  var out = [];
  for (var i = 0; i < records.length; i++) {
    var rec = records[i];
    if (SA_SKIP_IN_TRANSCRIPT[rec.kind]) continue;
    // A DONE carrying stop_reason="tool_use" ends one MODEL CALL, not
    // the turn: the loop runs the tools and calls the model again
    // (primer/session/timeline.py closes_turn makes the same cut).
    // Rendering them peppered the transcript with "done" markers
    // between every tool round and made the fold split one turn into
    // many (live finding 2026-08-26).
    if (rec.kind === "done"
        && ((rec.payload || {}).stop_reason === "tool_use")) continue;
    var kind = SA_KIND_TO_TRANSCRIPT[rec.kind] || "lifecycle";
    out.push({
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
    });
  }
  return out;
}

window.SA_SKIP_IN_TRANSCRIPT = SA_SKIP_IN_TRANSCRIPT;
window.SA_toTranscript = SA_toTranscript;
window.SA_KIND_TO_TRANSCRIPT = SA_KIND_TO_TRANSCRIPT;
