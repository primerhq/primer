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

  // Test surface, matching the classic adapter's __clientToolsExecutor.
  window.__shellClientToolsExecutor = executor;

  React.useEffect(function () {
    if (!sid) return undefined;
    executor.start(sid);
    var timer = window.setInterval(function () {
      executor.heartbeat(sid);
    }, SH_CT_HEARTBEAT_MS);
    return function () {
      window.clearInterval(timer);
      executor.stop(sid);
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
