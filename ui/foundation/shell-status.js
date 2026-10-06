// primer UI - the always-on status verb and the scroll-anchoring law
// (S8 spec section 8, "Status and streaming").
//
// One function produces the string so the composer status strip, the rail
// row chip and the tab label cannot drift apart. It is mounted the instant
// a send happens: with no tool call yet the verb is "thinking", never a
// bare spinner and never nothing.

var SH_FOLLOW_PX = 100;

// Bare tool names are scoped ("workspace__grep"); operators read verbs.
var SH_TOOL_VERB_ARG = ["path", "pattern", "query", "file", "command", "url"];

function SH_bareToolName(name) {
  var text = String(name || "");
  var idx = text.indexOf("__");
  return idx >= 0 ? text.slice(idx + 2) : text;
}

function SH_pad2(n) {
  return n < 10 ? "0" + n : String(n);
}

function SH_elapsedText(seconds) {
  var s = Math.max(0, Math.floor(seconds || 0));
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + "m " + SH_pad2(s % 60) + "s";
}

function SH_statusLine(status) {
  var st = status || {};
  var verb = st.verb || "thinking";
  var object = st.object ? " " + st.object : "";
  return "running: " + verb + object + " — " + SH_elapsedText(st.elapsedSec);
}

// UX reconcile wave 1 (audit A item 10): the OTHER status-strip form -
// a parked session waiting on a human decision. Mirrors SH_statusLine's
// own "one function, every altitude" rule so the composer status strip
// never drifts from wherever else this gets read. Takes the SAME item
// shape shell-attention.js's SH_toAttentionItems already produces
// ({kind: "approval"|"question", toolName}) - the gate item nv-session-
// doc.jsx's own gateItems list already carries this for the decision/ask
// cards, so wiring this in there is a straight swap for the previous
// generic "parked - waiting on {waiting_reason}" line, not new data.
function SH_waitLine(item) {
  if (!item) return null;
  // Live finding 01a064d3: item.toolName is the yield KIND ("approval"),
  // used elsewhere for tier routing - the gated TOOL id lives on
  // item.gatedTool (shell-attention.js's SH_gatedToolOf). Reading
  // toolName here rendered "waiting on approval — approval" instead of
  // "waiting on approval — workspace__write".
  var tool = item.gatedTool || (item.kind === "question" ? "ask_user" : "a tool call");
  var lead = item.kind === "question" ? "waiting on your answer" : "waiting on approval";
  return lead + " — " + tool + " (parked, worker released)";
}

// The full decision nv-session-doc.jsx's own waitNote prop needs: a
// gate-carrying park (approval/ask) names the tool via SH_waitLine
// above; a park with no gate item (a wake/timer park - session.
// waiting_reason has never been a real field on WorkspaceSession, so
// that branch's effective output today is always this same literal
// string) keeps its current wording untouched. gateItems is whatever
// window.SH_toAttentionItems(...) already produced for the decision/ask
// cards - this is a one-argument-shape drop-in for the current inline
// "parked — waiting on " + (session.waiting_reason || "a wake")
// expression, not new data collection.
function SH_parkedStatusLine(session, gateItems) {
  // Ticket 01a1072f: the deployment refuses this session's workspace, so its turn failed and the session is paused
  // (resumable, never lost). The reason lives on the row because the transcript is IN the refused workspace; it wins
  // over the park wording, since a refused resume leaves parked_status set, and applies to a never-parked session.
  if (session && session.workspace_refusal && session.status !== "ended") {
    return "workspace unavailable: " + session.workspace_refusal +
      " (session paused; resume it once the workspace is moved)";
  }
  if (!session || !session.parked_status) return null;
  // Live finding 01a064d3: a park can outlive its session (a sweep/
  // timeout continuation that then fails ends the session without
  // clearing parked_status), so parked_status alone is not sufficient -
  // an ended session is never still "waiting" on anything, no matter
  // what its stale parked_status column says. The ended state wins.
  if (session.status === "ended") return null;
  var gate = (gateItems || [])[0];
  return gate ? SH_waitLine(gate) : "parked — waiting on a wake";
}

// Walks the tap buffer for one session: the newest tool_call after the
// newest terminal wins. Returns null when the session is not running.
function SH_statusFromTap(events, sessionId, nowMs) {
  var current = null;
  for (var i = 0; i < (events || []).length; i++) {
    var ev = events[i];
    if (!ev || ev.session_id !== sessionId) continue;
    var kind = ev["class"];
    if (kind === "done" || kind === "cancelled" || kind === "error") {
      current = null;
      continue;
    }
    if (kind === "user_input") {
      current = { verb: "thinking", object: "", startedMs: SH_eventStartedMs(ev, nowMs) };
      continue;
    }
    if (kind === "tool_call") {
      var args = (ev.payload && ev.payload.arguments) || {};
      var object = "";
      for (var a = 0; a < SH_TOOL_VERB_ARG.length; a++) {
        var key = SH_TOOL_VERB_ARG[a];
        if (typeof args[key] === "string" && args[key]) {
          object = args[key];
          break;
        }
      }
      current = {
        verb: SH_bareToolName(ev.payload && ev.payload.name),
        object: object,
        startedMs: SH_eventStartedMs(ev, nowMs),
      };
    }
  }
  if (!current) return null;
  if (nowMs !== undefined && current.startedMs > nowMs) return null;
  return current;
}

// TapEvent.ts is an ISO datetime. The status line read a field named
// ts_ms, which that event does not have, so every start time fell back
// to 0 and the elapsed clock measured from the Unix epoch: a session
// that had just started reported "29787968m 43s" of thinking. An event
// with no readable timestamp is treated as starting now, so the clock
// reads 0s rather than fifty-five years.
function SH_eventStartedMs(ev, nowMs) {
  var raw = ev && ev.ts;
  if (raw) {
    var parsed = Date.parse(raw);
    if (!isNaN(parsed)) return parsed;
  }
  return nowMs === undefined ? 0 : nowMs;
}

function SH_scrollDecision(input) {
  var i = input || {};
  var distance = Number(i.distanceFromBottom || 0);
  var turns = Number(i.newTurns || 0);
  if (distance <= SH_FOLLOW_PX) {
    return { follow: true, showJump: false, jumpLabel: null };
  }
  if (turns <= 0) return { follow: false, showJump: false, jumpLabel: null };
  return {
    follow: false,
    showJump: true,
    jumpLabel: "Jump to latest - " + turns + (turns === 1 ? " new turn" : " new turns"),
  };
}

// Stop (interrupt): the acknowledged "stopping" state. The console used to show nothing
// after Stop, so an operator could not tell a lost click from a slow stop and reached for
// Close (Cancel), which ENDS the session. The state is derived from the SERVED flag:
// interrupt_requested is true from the moment the request is recorded until the worker lands
// the Stop (it then clears it and the session reads "waiting"). An ended session is never
// still stopping, whatever a stale flag on it says, and neither is a PARKED one: no turn is
// running while it waits, and the server refuses a Stop on it (409), so a leftover flag there
// is not a state the operator is waiting on.
function SH_isStopping(session) {
  return !!session && session.interrupt_requested === true
    && session.status !== "ended" && !session.parked_status;
}

// Can a Stop do anything? The server's own condition: POST .../interrupt records a Stop only on a RUNNING row
// that is not parked. Every other row answers 200 as a no-op (idle, waiting, paused, created) or 409 (parked,
// ended), so offering Stop there is an error or a lie. Every Stop entry point gates on this.
function SH_canStop(session) {
  return !!session && session.status === "running" && !session.parked_status;
}

// Verbs that mean "waiting on the model" (or an unknown phase): a Stop interrupts that wait
// at once, so the strip just says "stopping". Any other verb is a TOOL: a Stop cancels it (and waits for it to
// unwind, about three seconds at most), except a tool that must not be cancelled (a file write), which it waits for,
// up to five seconds. The strip does not know which, so it says the true common thing: the tool is being ended.
var SH_STOP_MODEL_WAIT_VERBS = {
  thinking: true, sending: true, responding: true, running: true, claimable: true,
};

function SH_stoppingLine(status) {
  var verb = (status && status.verb) || "thinking";
  if (SH_STOP_MODEL_WAIT_VERBS[verb]) return "stopping";
  if (verb === "executing") return "stopping: ending the running tool";
  return "stopping: ending " + verb;
}

// The slim lifecycle marker in the transcript. A Stop and a Cancel both write a "cancelled"
// record; only the reason tells them apart, and a Stop (the session stays alive) must not read
// as the end-of-session wording.
function SH_lifecycleLabel(kind, payload) {
  if (kind === "cancelled") {
    return payload && payload.reason === "operator_interrupt"
      ? "■ stopped" : "■ cancelled";
  }
  return "· " + kind;
}

window.SH_FOLLOW_PX = SH_FOLLOW_PX;
window.SH_bareToolName = SH_bareToolName;
window.SH_isStopping = SH_isStopping;
window.SH_canStop = SH_canStop;
window.SH_stoppingLine = SH_stoppingLine;
window.SH_lifecycleLabel = SH_lifecycleLabel;
window.SH_statusLine = SH_statusLine;
window.SH_waitLine = SH_waitLine;
window.SH_parkedStatusLine = SH_parkedStatusLine;
window.SH_eventStartedMs = SH_eventStartedMs;
window.SH_statusFromTap = SH_statusFromTap;
window.SH_scrollDecision = SH_scrollDecision;
