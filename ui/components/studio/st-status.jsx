// Studio revamp - the four-state status language (ui/studio/STUDIO-WIRING.md §4).
//
// Wraps, does not replace, ST_sessionStatus: the ~16 lifecycle/park values the
// backend can report collapse into four buckets an operator can scan, with the
// raw value kept in `detail` so an unmapped backend state is visible rather
// than silently rendering as "done".
//
// Pure logic, no React - so the mapping table can be exercised for real.

var ST2_BUCKET_ORDER = ["needs", "working", "broken", "done"];

// Park reasons where a HUMAN is the blocker.
var ST2_NEEDS_REASONS = { ask_user: 1, approval: 1, ask_approval: 1 };
// Park reasons that are technically parked but wait on a machine, not a person.
var ST2_QUIET_PARKS = { watch: 1, watch_files: 1, sleep: 1 };

function ST2_reasonCopy(reason, session) {
  var s = session || {};
  if (reason === "ask_user") return "asked a question";
  if (reason === "approval" || reason === "ask_approval") {
    var cmd = s.pending_command || s.tool || null;
    return cmd ? "approve · " + cmd : "waiting for approval";
  }
  if (reason === "watch" || reason === "watch_files") return "waiting on a file";
  if (reason === "sleep") return "sleeping until a timer fires";
  return reason ? String(reason) : "";
}

// ST2_bucketOf(session, opts) -> { bucket, tone, label, detail }
//   opts.pendingBySession: { [session_id]: true } from the workspace
//   pending-yield snapshot. A session can be RUNNING and still have an
//   unanswered yield, which is why the snapshot can promote it to "needs".
function ST2_bucketOf(session, opts) {
  var s = session || {};
  var o = opts || {};
  var status = String(s.status || "unknown");
  var reason = s.park_reason || s.parked_reason || (s.parked && s.parked.kind) || null;
  // The list endpoint returns SessionInfo, which carries `session_id`; the
  // create-response / detail shapes carry `id`. The pending snapshot is keyed
  // by `session_id`, so resolving only `id` would silently never promote a
  // row that came from the list - i.e. every row in the rail.
  var sid = s.session_id || s.id || null;
  var hasPendingYield = !!(o.pendingBySession && sid && o.pendingBySession[sid]);

  if ((status === "parked" && ST2_NEEDS_REASONS[reason]) || hasPendingYield) {
    return {
      bucket: "needs",
      tone: "--amber",
      label: "needs you",
      detail: ST2_reasonCopy(reason || "approval", s),
    };
  }

  var errCode = s.ended_detail || s.error_code || null;
  if (status === "failed" || ((status === "ended" || status === "cancelled") && errCode)) {
    return {
      bucket: "broken",
      tone: "--red",
      label: "broken",
      detail: errCode
        ? (s.tool ? String(errCode) + " · " + s.tool : String(errCode))
        : "run failed",
    };
  }

  if (status === "completed" || status === "ended" || status === "cancelled") {
    return {
      bucket: "done",
      tone: "--text-3",
      label: "done",
      detail: s.files_touched != null ? s.files_touched + " files touched" : "",
    };
  }

  // Everything else is "working", including the quiet parks and any value the
  // frontend has never heard of (kept verbatim in `detail`).
  var detail;
  if (status === "parked" && ST2_QUIET_PARKS[reason]) {
    detail = ST2_reasonCopy(reason, s);
  } else if (status === "running" || status === "created" || status === "resumable") {
    detail = s.substep || s.tool || "";
  } else {
    detail = status;
  }
  return { bucket: "working", tone: "--blue", label: "working", detail: detail || "" };
}

// Group a session list into the fixed bucket order. Callers sort WITHIN a
// group (ST_sessionSort) so the ordering rule stays in one place.
function ST2_groupSessions(sessions, opts) {
  var out = { needs: [], working: [], broken: [], done: [] };
  (sessions || []).forEach(function (s) {
    var b = ST2_bucketOf(s, opts);
    out[b.bucket].push(s);
  });
  return out;
}

window.ST2_bucketOf = ST2_bucketOf;
window.ST2_BUCKET_ORDER = ST2_BUCKET_ORDER;
window.ST2_reasonCopy = ST2_reasonCopy;
window.ST2_groupSessions = ST2_groupSessions;
