/* global React, SH_api, NV_useConsole, CT_createExecutor */
// Console host adapter for client tools (S3 spec section 5), ported
// from the sh shell on flag day. Headless: renders null. S3's
// dispatcher owns every rule (vocabulary, replay fence, duplicate
// delivery); this file owns host policy only:
//
//   1. openDoc -> con.setDoc as a preview tab. The nv doc host has one
//      active doc named by the URL, so a fully-background open is not
//      representable; an agent-driven open lands as a PREVIEW (italic,
//      replaced by the next single-click) rather than a promoted tab.
//   2. toast   -> the console toast.
//   3. attachLifecycle -> POST/DELETE .../attach around the mounted
//      session, heartbeating well inside the server's 30s TTL.
//
// Delivery frames ride the SHARED workspace tap. No second socket.

var NV_CT_HEARTBEAT_MS = 10000;

// One stable id per browser tab, so a reload is a NEW attachment (and
// therefore a new fence) rather than a heartbeat on the old one.
var NV_CT_CLIENT_ID = "console-" + Math.random().toString(36).slice(2, 12);

function NV_ClientTools(props) {
  var con = NV_useConsole();
  var sid = props.sid;
  var wid = con.wid;

  var executor = React.useMemo(function () {
    return CT_createExecutor({
      openDoc: function (kind, ref) {
        con.setDoc({ kind: kind, ref: ref });
      },
      toast: function (msg) { con.toast(msg); },
      attachLifecycle: {
        attach: function (sessionId) {
          return SH_api.attach(wid, sessionId, NV_CT_CLIENT_ID);
        },
        heartbeat: function (sessionId) {
          return SH_api.attach(wid, sessionId, NV_CT_CLIENT_ID);
        },
        detach: function (sessionId) {
          return SH_api.detach(wid, sessionId, NV_CT_CLIENT_ID);
        },
      },
    });
  }, [wid]);

  // Test surface (the ui_e2e client-tools journey reads it).
  window.__shellClientToolsExecutor = executor;

  React.useEffect(function () {
    if (!sid) return undefined;
    // Attaching is best effort: a session that has never run has no
    // on-disk slot, the server answers 404, and there is nothing for
    // the client to do about it.
    var quiet = function () {};
    Promise.resolve(executor.start(sid)).catch(quiet);
    var timer = window.setInterval(function () {
      Promise.resolve(executor.heartbeat(sid)).catch(quiet);
    }, NV_CT_HEARTBEAT_MS);
    return function () {
      window.clearInterval(timer);
      Promise.resolve(executor.stop(sid)).catch(quiet);
    };
  }, [sid, executor]);

  // Deliveries ride the shared workspace tap into the dispatcher.
  window.useWorkspaceTapListener(wid, function (ev) {
    executor.handleEvent(ev);
  });

  return null;
}

window.NV_CT_HEARTBEAT_MS = NV_CT_HEARTBEAT_MS;
window.NV_CT_CLIENT_ID = NV_CT_CLIENT_ID;
window.NV_ClientTools = NV_ClientTools;
