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
//      (payload.delegate_tool_call_id; crosscheck C1) and, for a record
//      that carries run ids, on the run that made the call
//      (payload.delegate_run_id / delegate_parent_run_id): a raw provider
//      call id is not unique, and a child's own call can reuse its
//      parent's.

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

// The id a delegated record names its delegating call by: the RAW provider id
// (DelegationRecorder stamps payload.delegate_tool_call_id with it). A real
// TOOL_CALL record carries it as payload.raw_id beside the scoped payload.id;
// one written before raw_id existed has only id (which WAS the raw id then);
// tool_call_id is the shape the early fixtures used. Reading only tool_call_id
// meant no real call was ever a parent and nothing nested.
function SH_callRawId(payload) {
  return payload.raw_id || payload.id || payload.tool_call_id || null;
}

function SH_nestSubagentRows(rows) {
  // byRun: "<run that made the call>|<raw id>" -> row ("" for a call the parent
  // turn itself made). Exact, used for a record that carries delegate_run_id.
  // byRaw: raw id -> the LAST call with it, for a record from before run ids.
  var byRun = {};
  var byRaw = {};
  var out = [];
  for (var i = 0; i < (rows || []).length; i++) {
    var row = rows[i];
    var payload = row.payload || {};
    var rawId = row.kind === "tool_call" ? SH_callRawId(payload) : null;
    var node = Object.assign({}, row, {
      children: rawId ? [] : (row.children || []),
    });
    var key = payload.delegate_tool_call_id;
    var target = null;
    if (key) {
      target = payload.delegate_run_id
        ? byRun[(payload.delegate_parent_run_id || "") + "|" + key]
        : byRaw[key];
    }
    if (target) {
      target.children.push(node);
    } else {
      out.push(node);
    }
    if (rawId) {
      // A delegated call is itself a parent for what ITS run delegates.
      byRaw[rawId] = node;
      byRun[(payload.delegate_run_id || "") + "|" + rawId] = node;
    }
  }
  return out;
}

// The run a call or a result belongs to, as the key that pairs them: a delegated record carries its run's id; a record from before run ids carries only the
// delegating call's raw id; the parent turn's own records carry neither. The scoped call id alone cannot pair them: the recorder numbers ids per run, so a child's call
// and its parent's are both x:tool:1:1 (review of #575, round 2). A delegated record that names no run and no delegating call belongs to the parent's scope here (""), where
// SA_failureScope (session-adapter.jsx) gives it a bucket of its own: that one keeps unattributed failures from folding into the parent's, this one pairs a call with a result.
function SH_callScope(payload) {
  if (payload.delegate_run_id) return payload.delegate_run_id;
  if (payload.delegated && payload.delegate_tool_call_id) return "call:" + payload.delegate_tool_call_id;
  return "";
}

// The transcript the console draws: the delegated rows nested under their calls, and the tool results indexed (by run and call id) so a call can find its own.
// The two are ONE step because the index is taken over the WHOLE transcript before nesting moves a delegated run's rows, its results included, under the call
// that delegated to it and out of the top level (the recorder writes a delegated tool_result like any other row).
function SH_nestWithResults(transcript) {
  var resultsByCallId = {};
  for (var ri = 0; ri < (transcript || []).length; ri++) {
    var res = transcript[ri];
    if (res.kind !== "tool_result") continue;
    var payload = res.payload || {};
    if (payload.call_id != null) resultsByCallId[SH_callScope(payload) + "|" + payload.call_id] = res;
  }
  return { flat: SH_nestSubagentRows(transcript), resultsByCallId: resultsByCallId };
}

// The result row that answers a tool call row, from SH_nestWithResults' index, or null while it is still running.
function SH_resultForCall(resultsByCallId, row) {
  var payload = row.payload || {};
  var id = payload.id || payload.tool_call_id || null;
  return id != null ? resultsByCallId[SH_callScope(payload) + "|" + id] || null : null;
}

// True when a failed call's error output ENDS with this notice's own words: the call ended ON this notice. A failed invoke_agent answers
// {"type", "message": "subagent 'x' LLM stream failed: <the stream error's message>"} (primer/toolset/system.py) and the resume path
// {"error": "subagent LLM stream failed: <the stream error's message>"} (primer/worker/frames.py), so the stream error's words END the text, at a word boundary.
// Anywhere else in the text they are just words: a short notice ("timeout") inside an unrelated failure did not end that call.
function SH_callQuotesNotice(callResult, notice) {
  var message = notice && notice.payload && typeof notice.payload.message === "string" ? notice.payload.message.trim() : "";
  var output = callResult && callResult.payload && callResult.payload.output;
  if (!message || typeof output !== "string") return false;
  var quoted = output;
  try {
    var body = JSON.parse(output);
    if (typeof body === "string") quoted = body;
    else if (body && typeof body === "object") quoted = body.message || body.error || output;
  } catch (_e) { quoted = output; }
  quoted = String(quoted).trim();
  var start = quoted.length - message.length;
  return start >= 0 && quoted.slice(start) === message && (start === 0 || !/\w/.test(quoted.charAt(start - 1)));
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

// Python's truthiness of a JSON value, which is what terminals.py tests with bool() / not: an empty list or object is falsy there and truthy in JavaScript, so
// a record with `delegated: []` or a marker with `message: []` would be judged differently by the server and the console without it.
function SH_pyTruthy(v) {
  if (v === null || v === undefined || v === false || v === 0 || v === "") return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v).length > 0;
  return true;
}

// A record's payload as an object: a log is written by many versions, and a payload that is not an object (a string, an array, a number, null) reads as an
// empty one, as terminals.payload_of does on the server.
function SH_payloadOf(rec) {
  var p = rec && rec.payload;
  return p && typeof p === "object" && !Array.isArray(p) ? p : {};
}

function SH_isTerminal(kind) {
  return SH_TERMINAL_KINDS.indexOf(kind) >= 0;
}

// Does this row end the SESSION's turn? The mirror of primer/session/terminals.py closes_turn, which the timeline endpoint
// numbers its turn windows by: the trace is asked for under the number counted here, so the two must agree. A subagent's
// terminal (payload.delegated) is the end of the subagent's turn and never the session's (ticket 01a11232); a model call
// that ended in a tool call ends a round, not the turn (the adapter already drops those rows; kept here so the rule is whole).
// An error that says, with an EXPLICIT fatal: false, that the stream went on from it is a notice inside the turn, not its end
// (ticket 01a11bf6); no flag, fatal: true and fatal: null still end it.
function SH_closesTurn(row) {
  if (!row || !SH_isTerminal(row.kind)) return false;
  var payload = SH_payloadOf(row);
  if (SH_pyTruthy(payload.delegated)) return false;
  if (row.kind === "error" && payload.fatal === false) return false;
  if (row.kind === "done" && payload.stop_reason === "tool_use") return false;
  return true;
}

// The instruction a failed turn can be sent again with (console review C-024), or null when a resend would be wrong or impossible. `flat` is the
// transcript's rows in order, `errorRow` the error card being drawn, `session` the polled row. The text of the user message that opened the turn
// is returned when: the row is an error; the session is not running (a send would queue behind a turn in flight); nothing the operator would be
// re-running over follows the error (a later user message, answer or tool call means the failure is history); and that message was plain text
// (attachments and non-text parts do not survive a resend of the text, so a Retry would be a different instruction). Markers after the error
// (the release `done`, the end divider) are not content.
var SH_RETRY_BLOCKING_AFTER = { user_message: true, assistant_message: true, tool_call: true };
function SH_retryInstruction(flat, errorRow, session) {
  if (!errorRow || errorRow.kind !== "error" || !session || session.session_state === "running") return null;
  var rows = flat || [];
  var at = -1;
  for (var i = rows.length - 1; i >= 0; i--) {
    if (rows[i].seq === errorRow.seq) { at = i; break; }
  }
  if (at < 0) return null;
  for (var j = at + 1; j < rows.length; j++) {
    if (SH_RETRY_BLOCKING_AFTER[rows[j].kind]) return null;
  }
  for (var k = at - 1; k >= 0; k--) {
    if (rows[k].kind !== "user_message") continue;
    var payload = rows[k].payload || {};
    var attached = (Array.isArray(payload.attachments) && payload.attachments.length) || (Array.isArray(payload.parts) && payload.parts.some(function (p) { return !p || p.type !== "text"; }));
    var text = typeof rows[k].label === "string" ? rows[k].label.trim() : "";
    return attached || !text ? null : text;
  }
  return null;
}

// The mirror of primer/session/terminals.py's TurnWindowScanner (ticket 01a11ca5). SH_closesTurn above is per record; one FAILED turn is one window however many
// error records it wrote (the stream's own, dispatch's with the same words, the claim adapter's release marker), and whether a record is a COPY of an earlier
// failure needs the records before it. Feed RAW records (kind, payload, node_id) in order; feed() says "closes" (the record ends a window), "copy" (a copy of a
// failure that already ended one: ends nothing, and belongs to the window it copies) or "inside". The rule, in the server's words: a delegated record is
// always inside; a user_input starts a new turn; a done / cancelled that closes the turn ends a window (done with stop_reason "error" is a FAILURE end that
// later errors of the turn can copy; any other end closes the turn); an error with an explicit fatal: false is a notice that ends nothing but whose words are
// remembered; a bare release marker is a copy once the turn has failed and the only evidence (so it ends the window) when nothing has; any other error ends a
// window unless the turn has already failed and an earlier error of the turn has the same non-empty message from the same node (a record that names no node
// matches any node's); dispatch's own failure ERROR (a title and an integer status, no fatal flag, no node) is a copy once the turn has failed, whatever its words,
// because records written before ticket 01a11f35-ad20 hold the stream's message raw while dispatch's copy is the problem detail, so comparing the words would
// take a copy for a new failure (old logs are not migrated). tests/ui/test_shell_turns.py compares the two
// over the shapes the writers produce.
var SH_WINDOW_CLOSES = "closes";
var SH_WINDOW_COPY = "copy";
var SH_WINDOW_INSIDE = "inside";

function SH_isBareMarker(rec) {
  var p = SH_payloadOf(rec);
  return !!rec && rec.kind === "error" && p.terminal === true && !SH_pyTruthy(p.message) && !SH_pyTruthy(p.code) && !SH_pyTruthy(p.title);
}

// The ERROR record dispatch's failure exit writes (terminals.is_dispatch_failure_record).
function SH_isDispatchFailureRecord(rec) {
  var p = SH_payloadOf(rec);
  return !!rec && rec.kind === "error" && typeof p.title === "string" && typeof p.status === "number" && isFinite(p.status) && Math.floor(p.status) === p.status
    && !Object.prototype.hasOwnProperty.call(p, "fatal") && !SH_pyTruthy(rec.node_id);
}

function SH_newWindowScanner() {
  var failed = false;
  var words = [];
  function newTurn() { failed = false; words = []; }
  function remember(message, node) { if (message) words.push([message, node]); }
  function copiesAnEarlierError(message, node) {
    if (!message) return false;
    for (var i = 0; i < words.length; i++) {
      if (words[i][0] === message && (!words[i][1] || !node || words[i][1] === node)) return true;
    }
    return false;
  }
  return {
    feed: function (rec) {
      var payload = SH_payloadOf(rec);
      if (SH_pyTruthy(payload.delegated)) return SH_WINDOW_INSIDE;
      if (rec.kind === "user_input") { newTurn(); return SH_WINDOW_INSIDE; }
      if (rec.kind === "error") {
        var message = typeof payload.message === "string" ? payload.message : null;
        var node = SH_pyTruthy(rec.node_id) ? rec.node_id : null;
        if (SH_isBareMarker(rec)) {
          if (failed) return SH_WINDOW_COPY;
          failed = true;
          return SH_WINDOW_CLOSES;
        }
        if (payload.fatal === false) { remember(message, node); return SH_WINDOW_INSIDE; }
        if (failed && (SH_isDispatchFailureRecord(rec) || copiesAnEarlierError(message, node))) return SH_WINDOW_COPY;
        remember(message, node);
        failed = true;
        return SH_WINDOW_CLOSES;
      }
      if (!SH_closesTurn(rec)) return SH_WINDOW_INSIDE;
      if (rec.kind === "done" && payload.stop_reason === "error") failed = true;
      else newTurn();
      return SH_WINDOW_CLOSES;
    },
  };
}

// The window each record is filed in, as the server's turn_windows files it: {of: {seq: 0-based window}, open: the index of the window still open (every
// window ended before it), first: the smallest seq seen}. A copy takes back to the window it copies everything written since that window ended. Pass ALL
// the raw records, rewound ones included (NOT SA_visibleRecords): the server counts every record, so a turn rewound away still takes its ordinal and the
// turns after it keep theirs; the rows the console draws are a subset of them.
function SH_windowsOfSeq(records) {
  var scanner = SH_newWindowScanner();
  var of = {};
  var windows = 0;
  var current = [];
  var first = Infinity;
  function file(recs, index) { for (var i = 0; i < recs.length; i++) of[recs[i].seq] = index; }
  for (var i = 0; i < (records || []).length; i++) {
    var rec = records[i];
    if (typeof rec.seq === "number" && rec.seq < first) first = rec.seq;
    var verdict = scanner.feed(rec);
    if (verdict === SH_WINDOW_COPY && windows > 0) {
      current.push(rec);
      file(current, windows - 1);
      current = [];
      continue;
    }
    current.push(rec);
    if (verdict === SH_WINDOW_CLOSES) {
      file(current, windows);
      windows += 1;
      current = [];
    }
  }
  file(current, windows);
  return { of: of, open: windows, first: first };
}

// 0-based turn ordinal per seq, matching the timeline endpoint's window ordinal. `windows` is SH_windowsOfSeq over the records the rows were drawn from: the
// ordinal is the window the record is filed in, so the console and the server number a failed turn (one window) and the turns after it the same. A row the
// records do not hold (an optimistic or live row) is numbered by where it sits: before the first record it is the first window's, after the last the open one's.
function SH_turnOfSeq(rows, windows) {
  var w = windows || { of: {}, open: 0, first: Infinity };
  var out = {};
  for (var i = 0; i < (rows || []).length; i++) {
    var seq = rows[i].seq;
    if (Object.prototype.hasOwnProperty.call(w.of, seq)) out[seq] = w.of[seq];
    else out[seq] = seq < w.first ? 0 : w.open;
  }
  return out;
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
window.SH_nestWithResults = SH_nestWithResults;
window.SH_resultForCall = SH_resultForCall;
window.SH_callQuotesNotice = SH_callQuotesNotice;
window.SH_collapseTurns = SH_collapseTurns;
window.SH_closesTurn = SH_closesTurn;
window.SH_turnOfSeq = SH_turnOfSeq;
window.SH_windowsOfSeq = SH_windowsOfSeq;
window.SH_newWindowScanner = SH_newWindowScanner;
window.SH_retryInstruction = SH_retryInstruction;
window.SH_shortTime = SH_shortTime;
window.SH_traceHeaderLabel = SH_traceHeaderLabel;
window.SH_thoughtLabel = SH_thoughtLabel;
window.SH_diffLineTone = SH_diffLineTone;
window.SH_looksLikeDiff = SH_looksLikeDiff;
