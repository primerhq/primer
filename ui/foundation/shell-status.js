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
  return "running: " + verb + object + " - " + SH_elapsedText(st.elapsedSec);
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
      current = { verb: "thinking", object: "", startedMs: ev.ts_ms || 0 };
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
        startedMs: ev.ts_ms || 0,
      };
    }
  }
  if (!current) return null;
  if (nowMs !== undefined && current.startedMs > nowMs) return null;
  return current;
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
window.SH_statusFromTap = SH_statusFromTap;
window.SH_scrollDecision = SH_scrollDecision;
