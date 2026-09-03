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

// Gate step one (live-verified against the restarted dev stack): GET
// /workspaces/{wid}/yields/pending's real "kind" field is "approval",
// never "ask_approval" - _extract_yield_kind (primer/api/routers/
// workspaces.py) maps the internal "_approval" tool name to the
// human-facing "approval". Before this fix a real pending approval
// gate never matched this allowlist and rendered "ambient" tier
// instead of "interrupt" - the exact case this tier split exists to
// catch.
var SH_DECISION_TOOLS = ["approval", "ask_user"];

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

// Live finding 01a064d3: item.toolName is deliberately the YIELD KIND
// ("approval"/"ask_user" - SH_tierFor/SH_isDecisionTool's tier-routing
// allowlist needs exactly that literal), NOT the gated tool's own id -
// but the decision card's "tool" chip (nv-session-doc.jsx's
// nv-card-tool) and the parked status strip (shell-status.js's
// SH_waitLine) both want the LATTER ("workspace__write", not
// "approval"). Same original_call.name SH_titleOf already reads,
// exposed as its own field so those two consumers stop reading
// toolName for a purpose it was never named for.
function SH_gatedToolOf(row) {
  if (SH_yieldKind(row) === "ask_user") return "ask_user";
  var call = ((row && row.resume_metadata) || {}).original_call || {};
  return call.name || null;
}

function SH_titleOf(row) {
  var call = ((row && row.resume_metadata) || {}).original_call || {};
  if (SH_yieldKind(row) === "ask_user") return "Question";
  return call.name ? "Approve " + call.name : "Approve tool call";
}

// UX reconcile wave 3 (audit A item 15): a normalized options list for
// an ask_user card's radio UI, or null for the free-text fallback (the
// SAME fallback the current textarea already is - _AskUserArgs.
// response_schema is optional, "Omit for free-text responses"). Reads
// jsonschema's own {enum: [...]} (any schema shape carrying it, e.g.
// bare or {type: "string", enum: [...]}) since that is what
// _validate_response_against_schema (primer/api/routers/yields.py)
// already enforces server-side; nothing here invents a new schema
// vocabulary. First entry is not pre-selected here - the reference
// mockup's own pre-selected option looks like a UI default (first-item),
// a rendering choice for whichever component consumes this, not data.
function SH_askOptionsOf(schema) {
  if (!schema || !Array.isArray(schema.enum) || !schema.enum.length) {
    return null;
  }
  var out = [];
  for (var i = 0; i < schema.enum.length; i++) {
    out.push({ value: schema.enum[i], label: String(schema.enum[i]) });
  }
  return out;
}

// UX reconcile wave 3 (audit A item 14): mirrors primer/model/
// tool_approval.py's ApproverSpec.allows(username, role) exactly - the
// backend's own authorization check, not a new rule invented here. This
// is an AFFORDANCE only (the backend re-checks on the real POST), so
// drifting from it is a wrong label, never a security regression.
function SH_viewerQualifies(approvers, viewer) {
  var v = viewer || {};
  if (v.role === "admin") return true;
  if (!approvers || approvers.kind === "anyone") return true;
  if (approvers.kind === "roles") {
    return (approvers.roles || []).indexOf(v.role) >= 0;
  }
  return (approvers.users || []).indexOf(v.username) >= 0;
}

// The approval card's routing label. "anyone" stays exactly as it reads
// today (qualifying is trivial and universal there, so personalizing it
// would be noise); a roles/users-scoped spec gets the reference's own
// form, "who may decide: {spec} — you qualify", with the qualifier
// dropped when the viewer's own role/username is not admitted - the
// dev stack runs auth-disabled (a single fixed "system"/"admin"
// identity), so every roles/users spec there always qualifies; this
// only shows real variation once real per-user roles exist.
function SH_routingLine(item, viewer) {
  var approvers = item && item.approvers;
  if (!approvers || approvers.kind === "anyone") return "anyone may decide";
  var spec = approvers.kind === "roles"
    ? (approvers.roles || []).join(", ")
    : (approvers.users || []).join(", ");
  var base = "who may decide: " + spec;
  return SH_viewerQualifies(approvers, viewer) ? base + " — you qualify" : base;
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
      gatedTool: SH_gatedToolOf(row),
      kind: SH_yieldKind(row) === "ask_user" ? "question" : "approval",
      title: SH_titleOf(row),
      // Kind decides the preview source (BDD pass 2026-08-24): a
      // QUESTION shows its prompt - the human-facing text - while an
      // approval shows the literal gated call from resume_metadata.
      // (Feeding resume_metadata first for both let the call shadow
      // the question the moment the route began carrying it.)
      preview: SH_previewOf(
        SH_yieldKind(row) === "ask_user"
          ? { prompt: row.prompt || (row.resume_metadata || {}).prompt }
          : (row.resume_metadata && row.resume_metadata.original_call
            ? row.resume_metadata
            : { prompt: row.prompt })
      ),
      at: row.parked_at || row.yielded_at,
      resolved: false,
      // Stamped by the cross-workspace fan-out so the Inbox can jump
      // to a session in another workspace (revamp section 5).
      workspaceId: row.workspace_id || null,
      // Who may decide (P6 approver routing); null = anyone. The
      // decision card renders this as its routing label.
      approvers: row.approvers
        || ((row.resume_metadata || {}).approvers)
        || null,
      // UX reconcile wave 5 (audit A item 15): _AskUserArgs.response_
      // schema is a real, jsonschema-enforced field (primer/toolset/
      // _system_tools.py) - already server-validated at POST time - and
      // primer/api/routers/workspaces.py's list_pending_yields /
      // list_session_pending_yields now include it in resume_metadata
      // (wave 5 backend fix). The row.responseSchema branch stays as a
      // forwards-compatible alias in case a future route flattens it to
      // the top level; the resume_metadata read is the one live routes
      // actually populate today. See SH_askOptionsOf below for the
      // {enum} shape an ask_user card's radio UI consumes from this.
      responseSchema: row.responseSchema
        || ((row.resume_metadata || {}).response_schema)
        || null,
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
      gatedTool: rec.tool_name,
      toolsetId: rec.toolset_id,
      kind: "approval",
      // The records endpoint's field is `decision` (approved/rejected);
      // `status` never existed on ToolApprovalRecord (live-pass finding,
      // M2 round - rec.status rendered "undefined <tool>").
      title: rec.decision + " " + rec.tool_name,
      preview: rec.reason || "",
      at: rec.decided_at,
      resolved: true,
      workspaceId: rec.workspace_id || null,
      // uiv2 Wave 3 (resolved-card renderer): explicit, undecorated
      // fields for NV_ResolvedDecisionCard to derive the notes §2.4
      // "approved/rejected by {user} · time — reason" line from - raw
      // ToolApprovalRecord shape, same fields the DECISIONS - AUDIT
      // table's derivation helpers already key off of.
      decision: rec.decision,
      decidedBy: rec.decided_by || null,
      reason: rec.reason || null,
      requestedAt: rec.requested_at || null,
      decidedAt: rec.decided_at || null,
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
    if (rec.decision !== "approved") continue;
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
window.SH_askOptionsOf = SH_askOptionsOf;
window.SH_viewerQualifies = SH_viewerQualifies;
window.SH_routingLine = SH_routingLine;
window.SH_approvedByMap = SH_approvedByMap;
window.SH_emptyTriage = SH_emptyTriage;
window.SH_triageKey = SH_triageKey;
window.SH_applyTriage = SH_applyTriage;
