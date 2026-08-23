// primer UI - attention items, computed (S8 spec section 8; amendment m10).
//
// The feed is S1's tap gate/pending envelopes plus the approvals
// endpoints. The event-routing spec's vocabulary is NEVER consulted here,
// and this file borrows none of its nouns, so that stays true.
//
// Tiers are routed by consequence (pinned decision 17): a yield that
// parks a session and needs a decision is an INTERRUPT; any other
// pending yield is AMBIENT; a resolved approval record is DIGEST.
// Interrupts are spent extremely sparingly, so the interrupt set is a
// two-element allowlist rather than a heuristic.

var SH_TIERS = ["interrupt", "ambient", "digest"];

var SH_DECISION_TOOLS = ["ask_approval", "ask_user"];

function SH_isDecisionTool(name) {
  for (var i = 0; i < SH_DECISION_TOOLS.length; i++) {
    if (SH_DECISION_TOOLS[i] === name) return true;
  }
  return false;
}

function SH_tierFor(item) {
  if (item && item.resolved) return "digest";
  if (item && SH_isDecisionTool(item.toolName)) return "interrupt";
  return "ambient";
}

// The literal command or diff the human is judging. A card without it is
// a free-text question, which section 8 forbids.
function SH_previewOf(meta) {
  var m = meta || {};
  if (m.preview) return String(m.preview);
  if (m.prompt) return String(m.prompt);
  var call = m.original_call || {};
  if (call.name) {
    var args = call.arguments || {};
    var parts = [];
    for (var key in args) {
      if (Object.prototype.hasOwnProperty.call(args, key)) {
        parts.push(key + "=" + String(args[key]));
      }
    }
    return call.name + "(" + parts.join(", ") + ")";
  }
  return "";
}

// GET /workspaces/{wid}/yields/pending answers rows of
// {session_id, kind, prompt, tool_call_id, parked_at}. This file was
// reading tool_name / resume_metadata / yielded_at, which that route
// has never sent: every parked question came through as a nameless
// "approval", so the rail could not tell a question from a tool call
// and never offered the operator a way to answer one.
function SH_yieldKind(row) {
  return (row && (row.kind || row.tool_name)) || "";
}

function SH_titleOf(row) {
  var call = ((row && row.resume_metadata) || {}).original_call || {};
  if (SH_yieldKind(row) === "ask_user") return "Question";
  return call.name ? "Approve " + call.name : "Approve tool call";
}

function SH_toAttentionItems(input) {
  var pending = (input && input.pending) || [];
  var records = (input && input.records) || [];
  var out = [];

  for (var i = 0; i < pending.length; i++) {
    var row = pending[i];
    var item = {
      id: "pending:" + row.tool_call_id,
      sessionId: row.session_id,
      toolCallId: row.tool_call_id,
      toolName: SH_yieldKind(row),
      kind: SH_yieldKind(row) === "ask_user" ? "question" : "approval",
      title: SH_titleOf(row),
      // The route sends the human-facing text as ``prompt``; the older
      // resume_metadata blob is still read when one is present.
      preview: SH_previewOf(row.resume_metadata || { prompt: row.prompt }),
      at: row.parked_at || row.yielded_at,
      resolved: false,
      // Stamped by the cross-workspace fan-out so the Inbox can jump
      // to a session in another workspace (revamp section 5).
      workspaceId: row.workspace_id || null,
    };
    item.tier = SH_tierFor(item);
    out.push(item);
  }

  for (var j = 0; j < records.length; j++) {
    var rec = records[j];
    var done = {
      id: "record:" + rec.id,
      sessionId: rec.session_id,
      toolCallId: rec.tool_call_id,
      toolName: rec.tool_name,
      kind: "approval",
      title: rec.status + " " + rec.tool_name,
      preview: rec.reason || "",
      at: rec.decided_at,
      resolved: true,
      workspaceId: rec.workspace_id || null,
    };
    done.tier = SH_tierFor(done);
    out.push(done);
  }

  return out;
}

// Which tool calls a human let through, keyed by call id. Rejections are
// excluded: a rejected call never executed, so nothing ran on anyone's
// behalf.
function SH_approvedByMap(records) {
  var out = {};
  for (var i = 0; i < (records || []).length; i++) {
    var rec = records[i];
    if (rec.status !== "approved") continue;
    out[rec.tool_call_id] = rec.decided_by || "you";
  }
  return out;
}

// Snooze and mute PERSIST but never delete: resolved and snoozed items
// stay queryable (section 8), so triage filters the live view only.
function SH_emptyTriage() {
  return { snoozedUntil: {}, mutedSessions: {} };
}

function SH_triageKey(username) {
  return "primer.shell.triage:" + String(username || "anon");
}

function SH_applyTriage(items, triage, nowMs) {
  var state = triage || SH_emptyTriage();
  var now = nowMs === undefined ? Date.now() : nowMs;
  var out = [];
  for (var i = 0; i < (items || []).length; i++) {
    var item = items[i];
    if (state.mutedSessions[item.sessionId]) continue;
    var until = state.snoozedUntil[item.id];
    if (until && until > now) continue;
    out.push(item);
  }
  return out;
}

window.SH_TIERS = SH_TIERS;
window.SH_tierFor = SH_tierFor;
window.SH_yieldKind = SH_yieldKind;
window.SH_toAttentionItems = SH_toAttentionItems;
window.SH_approvedByMap = SH_approvedByMap;
window.SH_emptyTriage = SH_emptyTriage;
window.SH_triageKey = SH_triageKey;
window.SH_applyTriage = SH_applyTriage;
