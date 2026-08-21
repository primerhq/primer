/* global React, SH_api, SH_useShell, CT_createExecutor */
// Fresh-shell host adapter for client tools (S3 spec section 5).
//
// Headless: renders null. S3's dispatcher owns every rule (vocabulary,
// the replay fence, duplicate delivery); this file owns only host
// policy, which is three things:
//
//   1. openDoc -> shell.openDoc as a BACKGROUND PREVIEW tab. Section 8
//      is explicit: agent-driven opens get a badge and a narration line,
//      never focus, and never a fresh tab per narration.
//   2. toast   -> the shell's toast entry point.
//   3. attachLifecycle -> POST/DELETE .../attach around the mounted
//      session, heartbeating well inside the server's 30s TTL.
//
// Delivery frames ride the SHARED workspace tap. No second socket.

var SH_CT_HEARTBEAT_MS = 10000;

// One stable id per browser tab, so a reload is a NEW attachment (and
// therefore a new fence) rather than a heartbeat on the old one.
var SH_CT_CLIENT_ID = "shell-" + Math.random().toString(36).slice(2, 12);

function SH_ClientTools(props) {
  var shell = SH_useShell();
  var sid = props.sid;
  var wid = shell.wid;

  var executor = React.useMemo(function () {
    return CT_createExecutor({
      openDoc: function (kind, ref, line) {
        // Background preview + badge: no focus theft, no tab creep.
        shell.openDoc({
          kind: kind, ref: ref, preview: true, focus: false,
          title: line ? ref + ":" + line : undefined,
        });
      },
      toast: function (msg) { shell.toast(msg); },
      attachLifecycle: {
        attach: function (sessionId) {
          return SH_api.attach(wid, sessionId, SH_CT_CLIENT_ID);
        },
        heartbeat: function (sessionId) {
          return SH_api.attach(wid, sessionId, SH_CT_CLIENT_ID);
        },
        detach: function (sessionId) {
          return SH_api.detach(wid, sessionId, SH_CT_CLIENT_ID);
        },
      },
    });
  }, [wid]);

  // Test surface. Named for the shell, like its other globals: the
  // classic adapter it replaced published __clientToolsExecutor, and a
  // comment claiming these match sent the journey looking for a name
  // nothing defines.
  window.__shellClientToolsExecutor = executor;

  React.useEffect(function () {
    if (!sid) return undefined;
    // Attaching is best effort and always has been: a session that has
    // never run has no on-disk slot, so the server answers 404 and there
    // is nothing for the client to do about it. Unhandled, that rejection
    // lands in the console of every journey that opens a not-yet-started
    // session and buries the errors worth reading.
    var quiet = function () {};
    Promise.resolve(executor.start(sid)).catch(quiet);
    var timer = window.setInterval(function () {
      Promise.resolve(executor.heartbeat(sid)).catch(quiet);
    }, SH_CT_HEARTBEAT_MS);
    return function () {
      window.clearInterval(timer);
      Promise.resolve(executor.stop(sid)).catch(quiet);
    };
  }, [executor, sid]);

  window.useWorkspaceTapListener(wid, function (ev) {
    executor.handleEvent(ev);
  });

  return null;
}

window.SH_CT_HEARTBEAT_MS = SH_CT_HEARTBEAT_MS;
window.SH_CT_CLIENT_ID = SH_CT_CLIENT_ID;
window.SH_ClientTools = SH_ClientTools;
