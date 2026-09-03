// primer UI - turn presentation rules (S8 spec section 8).
//
// Operates on the row shape SA_toTranscript already produces
// (ui/components/session-adapter.jsx:79-94): {seq, kind, nodeId, label,
// payload, createdAt}. Three rules live here:
//
//   1. Tool chips speak plain language and NEVER carry raw args; the
//      trace tab is the exhaustive record.
//   2. A finished turn collapses to named sections plus the final
//      message, so a long session reads as a list of answers. The live
//      turn stays fully expanded.
//   3. Subagent rows nest under the delegating tool call, keyed on the
//      attribution S1 writes into the parent messages.jsonl
//      (payload.delegate_tool_call_id; crosscheck C1).

var SH_TOOL_VERBS = {
  grep: { verb: "searched", tone: "read" },
  search: { verb: "searched", tone: "read" },
  web_search: { verb: "searched", tone: "read" },
  read_file: { verb: "read", tone: "read" },
  read_doc_content: { verb: "read", tone: "read" },
  list_dir: { verb: "listed", tone: "read" },
  write_file: { verb: "wrote", tone: "write" },
  edit_file: { verb: "edited", tone: "write" },
  move_file: { verb: "moved", tone: "write" },
  delete_file: { verb: "deleted", tone: "write" },
  run_command: { verb: "ran", tone: "write" },
  invoke_agent: { verb: "delegated to", tone: "other" },
  invoke_graph: { verb: "ran graph", tone: "other" },
  switch_binding: { verb: "switched to", tone: "other" },
  open_file: { verb: "opened", tone: "other" },
  ask_user: { verb: "asked", tone: "other" },
};

// One argument, chosen by name, never the argument object.
var SH_CHIP_ARGS = ["path", "pattern", "query", "file", "command",
                    "agent_id", "graph_id", "url", "prompt"];

function SH_chipObject(args) {
  var a = args || {};
  for (var i = 0; i < SH_CHIP_ARGS.length; i++) {
    var value = a[SH_CHIP_ARGS[i]];
    if (typeof value === "string" && value) {
      return value.length > 60 ? value.slice(0, 59) + "…" : value;
    }
  }
  return "";
}

// UX reconcile wave 7 (audit A items 4/6, render half): resultRow is the
// paired tool_result row (NV_ToolBlock's props.result), optional and
// absent while the call is still running - the args-only label below is
// exactly what a running/historical/pre-wave-5 call already showed, so
// omitting it (or passing one with no usable metadata) is a no-op, not
// a regression.
function SH_toolChipLabel(row, resultRow) {
  var payload = (row && row.payload) || {};
  var bare = window.SH_bareToolName(payload.name);
  var spec = SH_TOOL_VERBS[bare] || { verb: "ran " + bare, tone: "other" };
  var object = SH_chipObject(payload.arguments);
  var label = spec.verb;
  // A write chip names the file it touched, and clicking it must open
  // that file rather than re-deriving the path from the rendered label.
  // Lifted only for writes: a read chip's object is already the answer.
  var args = payload.arguments || {};
  var path = spec.tone === "write" && typeof args.path === "string"
    ? args.path
    : null;
  var argForm = object ? label + " " + object : label;
  // Once a result carries wave 5's exact server metadata, prefer a
  // result-aware label over the args-only guess above - "searched 42
  // files" says what happened; "searched webhook" only says what was
  // asked. The two accessors are shape-driven (grep's file_count vs
  // write/edit's additions/deletions), not tool-name-driven, so this
  // dispatches correctly without hardcoding which bare tool is which.
  var resultLabel = argForm;
  if (resultRow) {
    var countLabel = window.SA_resultCountLabel(resultRow);
    if (countLabel) {
      resultLabel = countLabel;
    } else {
      var stat = window.SA_diffStatOfResult(resultRow);
      if (stat) {
        resultLabel = argForm + " +" + stat.additions + " -" + stat.deletions;
      }
    }
  }
  return {
    label: resultLabel,
    tone: spec.tone,
    path: path,
  };
}

function SH_nestSubagentRows(rows) {
  var byCallId = {};
  var out = [];
  for (var i = 0; i < (rows || []).length; i++) {
    var row = rows[i];
    var payload = row.payload || {};
    if (row.kind === "tool_call" && payload.tool_call_id) {
      var parent = Object.assign({}, row, { children: [] });
      byCallId[payload.tool_call_id] = parent;
      out.push(parent);
      continue;
    }
    var key = payload.delegate_tool_call_id;
    if (key && byCallId[key]) {
      byCallId[key].children.push(row);
      continue;
    }
    out.push(Object.assign({}, row, { children: row.children || [] }));
  }
  return out;
}

// UX reconcile wave 2 (audit A item 2): a short local-time label for a
// turn's byline, next to the name. Same format as shared/transcript.jsx's
// CT_formatTime (that file is not in this task's boundary, so this is a
// small duplicate rather than a cross-file import) - "" for a
// missing/unparsable createdAt so callers can render conditionally
// without flashing "Invalid Date".
function SH_shortTime(createdAt) {
  if (!createdAt) return "";
  var d = new Date(createdAt);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// UX reconcile wave 2 (audit A item 5): the trace split's header - "trace
// · turn N" names the turn but says nothing about what happened in it.
// Enrich to "trace · {calls} · {span}" using the SAME node list the trace
// body renders below it (the turn's timeline nodes, from the /timeline
// endpoint - see NV_TraceSplit/NV_TraceMaximize's flatRows) rather than
// the transcript's own turnRows: SA_toTranscript deliberately skips
// llm_call records (SA_SKIP_IN_TRANSCRIPT in session-adapter.jsx - "the
// Trace panel reads it from the timeline endpoint"), so turnRows can
// never contain them and a header counting only over turnRows silently
// undercounts against a body that renders both tool_call AND llm_call
// rows (live finding 01a064d3: "1 CALL" above three visible rows).
// N = tool_call + llm_call node count (every row the body actually
// shows a T/A glyph for - see NV_traceGlyph); span = latest node end
// (ts + duration_ms) minus earliest node start, using each node's own
// authoritative duration_ms (primer/session/timeline.py already
// computes it per node via _delta_ms) rather than reconstructing it
// from paired tool_call/tool_result timestamps. A turn with no
// tool/llm calls (pure reasoning + answer, or nothing yet) keeps the
// plain "turn N" form - there is nothing to count.
function SH_traceHeaderLabel(turnNo, traceNodes) {
  var callCount = 0;
  var minMs = null;
  var maxMs = null;
  for (var i = 0; i < (traceNodes || []).length; i++) {
    var node = traceNodes[i];
    if (node.kind !== "tool_call" && node.kind !== "llm_call") continue;
    callCount += 1;
    if (!node.ts) continue;
    var startMs = Date.parse(node.ts);
    if (isNaN(startMs)) continue;
    var endMs = startMs
      + (typeof node.duration_ms === "number" ? node.duration_ms : 0);
    if (minMs === null || startMs < minMs) minMs = startMs;
    if (maxMs === null || endMs > maxMs) maxMs = endMs;
  }
  if (!callCount) return "trace · turn " + turnNo;
  var callsLabel = callCount + (callCount === 1 ? " call" : " calls");
  if (minMs === null || maxMs === null) return "trace · " + callsLabel;
  var seconds = Math.max(0, Math.round((maxMs - minMs) / 1000));
  return "trace · " + callsLabel + " · " + seconds + "s";
}

// UX reconcile wave 4 (audit A item 3): NV_Thought's collapsed label was
// the literal word "thought" plus a raw 110-char peek - the reference
// shows short semantic summaries instead ("Explored the repo", "Chose
// the handler seam"). True summarization needs an LLM, out of scope
// here (do not build that) - this is an honest heuristic approximation
// only: reasoning text often opens with a topic sentence, so the FIRST
// sentence (trimmed, ellipsized) reads close enough for a collapsed
// label most of the time. Full semantic summaries remain a product-
// level follow-up, not something this heuristic claims to solve.
function SH_thoughtLabel(text) {
  var trimmed = String(text || "").trim();
  if (!trimmed) return "thought";
  var m = trimmed.match(/^[^.!?\n]+[.!?]?/);
  var sentence = (m ? m[0] : trimmed).trim();
  if (sentence.length > 60) sentence = sentence.slice(0, 59) + "…";
  return sentence || "thought";
}

// UX reconcile wave 4 (audit A item 14 partial): approval-card previews
// sometimes carry a unified diff (a file-edit tool call awaiting
// approval) - nv-file-docs.jsx's NV_DiffDoc already colors +/- lines
// this same one-line way, but that file is a component (not an
// importable pure function) and outside this task's boundary, so this
// is a small duplicate rather than a cross-file reach, same drift-note
// precedent as SH_shortTime above.
function SH_diffLineTone(line) {
  var ch = String(line || "").charAt(0);
  return ch === "+" ? "add" : ch === "-" ? "del" : "ctx";
}

// A preview is diff-shaped only when it carries an actual unified-diff
// hunk header ("@@ ... @@") - gating on a bare "+"/"-" prefix alone
// would misfire on ordinary text (a bullet list line starting with "-"
// is not rare), coloring content that was never a diff.
function SH_looksLikeDiff(text) {
  var lines = String(text || "").split("\n");
  for (var i = 0; i < lines.length; i++) {
    if (/^@@ /.test(lines[i])) return true;
  }
  return false;
}

var SH_TERMINAL_KINDS = ["done", "cancelled", "error"];

function SH_isTerminal(kind) {
  return SH_TERMINAL_KINDS.indexOf(kind) >= 0;
}

function SH_sectionLabel(group) {
  var parts = [];
  for (var i = 0; i < group.length; i++) {
    if (group[i].kind !== "tool_call") continue;
    parts.push(SH_toolChipLabel(group[i]).label);
  }
  return parts.join(", ");
}

// liveFromSeq is the first seq of the turn currently running; rows at or
// above it are left untouched (phase one), everything below folds
// (phase two).
function SH_collapseTurns(rows, opts) {
  var liveFrom = (opts && opts.liveFromSeq) !== undefined
    ? opts.liveFromSeq : Infinity;
  var out = [];
  var buffer = [];

  function flush() {
    if (!buffer.length) return;
    out.push({
      kind: "section",
      label: SH_sectionLabel(buffer),
      count: buffer.length,
      rows: buffer.slice(),
      seq: buffer[0].seq,
    });
    buffer = [];
  }

  for (var i = 0; i < (rows || []).length; i++) {
    var row = rows[i];
    if (row.seq >= liveFrom) {
      flush();
      out.push(row);
      continue;
    }
    // Tool traffic and model thinking are the finished-turn detail a
    // reader scans past. "reasoning" collapses with them rather than
    // sitting between answers as a wall of prose, and an
    // external_tool_call has already been mapped to the tool_call row
    // shape by the adapter, so it needs no branch of its own here: the
    // pair folds into one section exactly like an internal call.
    if (
      row.kind === "tool_call"
      || row.kind === "tool_result"
      || row.kind === "reasoning"
    ) {
      buffer.push(row);
      continue;
    }
    flush();
    out.push(row);
    if (SH_isTerminal(row.kind)) flush();
  }
  flush();
  return out;
}

window.SH_TOOL_VERBS = SH_TOOL_VERBS;
window.SH_toolChipLabel = SH_toolChipLabel;
window.SH_nestSubagentRows = SH_nestSubagentRows;
window.SH_collapseTurns = SH_collapseTurns;
window.SH_shortTime = SH_shortTime;
window.SH_traceHeaderLabel = SH_traceHeaderLabel;
window.SH_thoughtLabel = SH_thoughtLabel;
window.SH_diffLineTone = SH_diffLineTone;
window.SH_looksLikeDiff = SH_looksLikeDiff;
