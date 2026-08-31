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
  var tool = item.toolName || (item.kind === "question" ? "ask_user" : "a tool call");
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
  if (!session || !session.parked_status) return null;
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

window.SH_FOLLOW_PX = SH_FOLLOW_PX;
window.SH_bareToolName = SH_bareToolName;
window.SH_statusLine = SH_statusLine;
window.SH_waitLine = SH_waitLine;
window.SH_parkedStatusLine = SH_parkedStatusLine;
window.SH_eventStartedMs = SH_eventStartedMs;
window.SH_statusFromTap = SH_statusFromTap;
window.SH_scrollDecision = SH_scrollDecision;
