// client-tools.js - the client-tool executor (S3 spec section 5).
//
// One dispatcher for agent-driven UI actions, built as a STANDALONE module
// with an injected host interface so every shell can adapt it to its own
// surfaces:
//
//   host.openDoc(kind, ref, line)  open a document (kind "file" in v1)
//   host.toast(msg)                surface a one-way message
//   host.attachLifecycle           {attach, heartbeat, detach}, host-driven
//
// Focus and presentation policy (which tab is foregrounded, how an open is
// surfaced) belongs to the HOST, not here.
//
// The replay fence: each attachment carries the session's high-water seq at
// attach time. Only records ABOVE the current attachment's mark are
// executed; anything at or below arrived through tap Last-Event-ID replay
// or a history read and is rendered only. A per-attachment executed set
// additionally makes a mid-attachment redelivery of the same frame a no-op,
// so "executes once" holds literally.
//
// No-build scope rules: top-level declarations use `var`; helpers are
// prefixed CT_ to avoid global collisions; exported symbols go on window.
// This file names no app surface, no URL, and no React: it is pure logic.

var CT_OPEN_FILE = "open_file";
var CT_INFORM_USER = "inform_user";
var CT_EVENT_CLASS = "client_action";

// Tool ids arrive SCOPED (toolset_id__bare_name). Match on the bare name so
// the same vocabulary works whichever toolset id serves it.
function CT_bareName(name) {
  var s = String(name == null ? "" : name);
  var idx = s.lastIndexOf("__");
  return idx < 0 ? s : s.slice(idx + 2);
}

function CT_createExecutor(host) {
  // sessionId -> {mark: int, done: {seq: true}}
  var attachments = {};

  function setAttachment(sessionId, attachedSeq) {
    if (!sessionId) return;
    var mark = Number(attachedSeq);
    attachments[sessionId] = {
      mark: isFinite(mark) ? mark : 0,
      done: {},
    };
  }

  function clearAttachment(sessionId) {
    if (sessionId) delete attachments[sessionId];
  }

  function run(name, args) {
    var bare = CT_bareName(name);
    if (bare === CT_OPEN_FILE) {
      if (!args || typeof args.path !== "string" || !args.path) return false;
      host.openDoc("file", args.path, args.line);
      return true;
    }
    if (bare === CT_INFORM_USER) {
      if (!args || typeof args.message !== "string" || !args.message) return false;
      host.toast(args.message);
      return true;
    }
    // Vocabulary the console does not know yet: transcript-only.
    return false;
  }

  function handleEvent(ev) {
    if (!ev || typeof ev !== "object") return "ignored";
    if (ev["class"] !== CT_EVENT_CLASS) return "ignored";
    var att = attachments[ev.session_id];
    if (!att) return "ignored";
    var seq = Number(ev.seq);
    if (!isFinite(seq) || seq <= att.mark) return "rendered";
    if (att.done[seq]) return "rendered";
    var payload = ev.payload && typeof ev.payload === "object" ? ev.payload : {};
    var args = payload.arguments && typeof payload.arguments === "object"
      ? payload.arguments
      : {};
    if (!run(payload.name, args)) return "ignored";
    att.done[seq] = true;
    return "executed";
  }

  // Lifecycle sugar: the HOST owns the timer and the transport; these just
  // keep the fence in step with what the host's calls returned.
  function start(sessionId) {
    return Promise.resolve(host.attachLifecycle.attach(sessionId)).then(
      function (res) {
        var seq = res && res.attached_seq !== undefined ? res.attached_seq : 0;
        setAttachment(sessionId, seq);
        return res;
      }
    );
  }

  function heartbeat(sessionId) {
    return Promise.resolve(host.attachLifecycle.heartbeat(sessionId));
  }

  function stop(sessionId) {
    clearAttachment(sessionId);
    return Promise.resolve(host.attachLifecycle.detach(sessionId));
  }

  return {
    setAttachment: setAttachment,
    clearAttachment: clearAttachment,
    handleEvent: handleEvent,
    start: start,
    heartbeat: heartbeat,
    stop: stop,
  };
}

window.CT_createExecutor = CT_createExecutor;
window.CT_OPEN_FILE = CT_OPEN_FILE;
window.CT_INFORM_USER = CT_INFORM_USER;
