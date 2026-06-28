/* global React */
// Shared session-state decoder + countdown. ONE source of truth for "what
// state is this session in, in plain language", consumed by both the
// sessions list (row chips + filters) and the session detail page (outcome
// banner + waiting line). Pure: derives only from fields already on the
// session row returned by GET /sessions. No backend call.

// Known graph/agent ended_detail codes -> human text. Unknown codes are
// returned verbatim (the log is observability data, not a closed contract).
const _SS_ENDED_DETAIL = {
  routing_failed: "a conditional edge matched no branch",
  max_iterations_exceeded: "hit the iteration cap",
  node_failed: "a node failed",
  fanin_upstream_failed: "an upstream branch failed before fan-in",
  tool_execution_failed: "a tool call failed",
  any_failed: "a required branch failed",
  begin_input_invalid: "invalid begin input",
  template_error: "the End output template failed to render",
};

function _ssDecodeEndedDetail(code) {
  if (!code) return null;
  return _SS_ENDED_DETAIL[code] || String(code);
}

// parked_event_keys carry a prefix that says what the park is waiting on.
function _ssWaitingOn(session) {
  const keys = session.parked_event_keys
    || (session.parked_event_key ? [session.parked_event_key] : []);
  for (const k of keys) {
    if (typeof k !== "string") continue;
    if (k.startsWith("ask_user:")) return "input";
    if (k.startsWith("tool_approval:") || k.startsWith("approval:")) return "approval";
    if (k.startsWith("timer:") || k.startsWith("sleep:")) return "timer";
    if (k.startsWith("watch:")) return "watch";
    if (k.startsWith("mcp_task:")) return "task";
  }
  return null;
}

const _SS_WAIT_LABEL = {
  input: "input", approval: "approval", timer: "a timer",
  watch: "file changes", task: "a background task",
};

function describeSessionState(session) {
  if (!session) return { group: "idle", label: "—", tone: "neutral", detail: null, needsAttention: false, countdownTo: null, waitingOn: null };
  const status = session.status;
  const reason = session.ended_reason;

  // Terminal: outcome. Check failure/cancel BEFORE "completed" so an
  // ended-but-failed (or ended-but-cancelled) row never reads "Completed".
  // (failed / cancelled may arrive as a status OR only as an ended_reason.)
  if (status === "failed" || reason === "failed") {
    return { group: "failed", label: "Failed", tone: "red", detail: _ssDecodeEndedDetail(session.ended_detail), needsAttention: false, countdownTo: null, waitingOn: null };
  }
  if (reason === "workspace_lost") {
    return { group: "failed", label: "Workspace lost", tone: "red", detail: _ssDecodeEndedDetail(session.ended_detail), needsAttention: false, countdownTo: null, waitingOn: null };
  }
  if (status === "cancelled" || reason === "cancelled") {
    return { group: "cancelled", label: "Cancelled", tone: "neutral", detail: null, needsAttention: false, countdownTo: null, waitingOn: null };
  }
  if (reason === "force_deleted") {
    return { group: "cancelled", label: "Force-deleted", tone: "neutral", detail: null, needsAttention: false, countdownTo: null, waitingOn: null };
  }
  if (status === "ended" || status === "completed" || reason === "completed") {
    return { group: "ended", label: "Completed", tone: "green", detail: null, needsAttention: false, countdownTo: null, waitingOn: null };
  }

  // Parked / waiting.
  if (session.parked_status === "parked" || status === "waiting") {
    const waitingOn = _ssWaitingOn(session);
    return {
      group: "waiting",
      label: "Waiting on " + (_SS_WAIT_LABEL[waitingOn] || "an event"),
      tone: "amber",
      detail: null,
      needsAttention: waitingOn === "input" || waitingOn === "approval",
      countdownTo: session.parked_until || null,
      waitingOn,
    };
  }

  if (status === "paused") {
    return { group: "waiting", label: "Paused", tone: "amber", detail: null, needsAttention: false, countdownTo: null, waitingOn: null };
  }
  if (status === "running") {
    const turn = session.turn_no != null ? session.turn_no : (session.turn_count != null ? session.turn_count : 0);
    return { group: "running", label: "Running", tone: "blue", detail: "turn " + turn, needsAttention: false, countdownTo: null, waitingOn: null };
  }
  if (status === "created") {
    return { group: "idle", label: "Awaiting worker", tone: "neutral", detail: null, needsAttention: false, countdownTo: null, waitingOn: null };
  }
  return { group: "idle", label: String(status || "—"), tone: "neutral", detail: null, needsAttention: false, countdownTo: null, waitingOn: null };
}

// Live countdown to an ISO target. Ticks once/sec. null target renders
// nothing; at/after zero renders "any moment now".
function SessionCountdown({ to, prefix }) {
  const [, force] = React.useState(0);
  React.useEffect(() => {
    if (!to) return undefined;
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [to]);
  if (!to) return null;
  const ms = new Date(to).getTime() - Date.now();
  let text;
  if (!(ms > 0)) {
    text = "any moment now";
  } else {
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    text = m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
  }
  return <span className="muted text-sm mono">{(prefix || "") + text}</span>;
}

window.describeSessionState = describeSessionState;
window.SessionCountdown = SessionCountdown;
