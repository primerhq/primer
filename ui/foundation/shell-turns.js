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

function SH_toolChipLabel(row) {
  var payload = (row && row.payload) || {};
  var bare = window.SH_bareToolName(payload.name);
  var spec = SH_TOOL_VERBS[bare] || { verb: "ran " + bare, tone: "other" };
  var object = SH_chipObject(payload.arguments);
  var label = SH_TOOL_VERBS[bare] ? spec.verb : spec.verb;
  return {
    label: object ? label + " " + object : label,
    tone: spec.tone,
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
