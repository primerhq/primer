/* global React, CT_createExecutor, ST2_api */
// Classic Studio host adapter for client tools (S3 spec section 5).
//
// Headless: renders null. It owns exactly three things, all of them host
// policy, and delegates every rule to foundation/client-tools.js:
//
//   1. openDoc -> studio.openTab, whose id-keyed dedupe is what makes
//      duplicate / multi-client delivery harmless.
//   2. toast   -> window.primerApi.toastPush, the single toast entry
//      point callable outside React (app.jsx).
//   3. attachLifecycle -> POST/DELETE .../attach around the mounted
//      session, heartbeating well inside the server's 30s TTL.
//
// Delivery frames arrive on the SHARED workspace tap; no second socket.

// A third of the server's ATTACH_TTL_SECONDS, so one dropped beat is
// survivable.
var ST_CT_HEARTBEAT_MS = 10000;

// One stable id per browser tab, so a reload is a NEW attachment (and
// therefore a new fence) rather than a heartbeat on the old one.
var ST_CT_CLIENT_ID = "tab-" + Math.random().toString(36).slice(2, 12);

function ST_ClientTools({ wid, sid, studio }) {
  var studioRef = React.useRef(studio);
  studioRef.current = studio;

  var executor = React.useMemo(function () {
    return CT_createExecutor({
      openDoc: function (kind, ref, line) {
        if (kind !== "file") return;
        var st = studioRef.current;
        if (!st || typeof st.openTab !== "function") return;
        var tabId = "file:" + ref;
        var openList = (st.state && st.state.openTabs) || [];
        var existed = openList.some(function (t) { return t.id === tabId; });
        st.openTab({
          id: tabId,
          kind: "file",
          ref: ref,
          title: String(ref).split("/").pop() || ref,
          line: line || null,
        });
        // openTab dedupes on tab.id, and that dedupe is exactly what makes
        // duplicate / multi-client delivery harmless. The cost is that an
        // ALREADY-OPEN tab keeps its old record, so a second open_file at a
        // different line would never move the viewer. Patch the line onto
        // it. Safe against the stale read: this branch only runs when the
        // tab is already in the array openTab just left untouched.
        if (existed && typeof st.patch === "function") {
          st.patch({
            openTabs: openList.map(function (t) {
              return t.id === tabId
                ? Object.assign({}, t, { line: line || null })
                : t;
            }),
          });
        }
      },
      toast: function (msg) {
        var push = window.primerApi && window.primerApi.toastPush;
        if (typeof push === "function") push({ kind: "info", title: msg });
      },
      attachLifecycle: {
        attach: function (sessionId) {
          return ST2_api.attachClient(wid, sessionId, ST_CT_CLIENT_ID);
        },
        heartbeat: function (sessionId) {
          return ST2_api.attachClient(wid, sessionId, ST_CT_CLIENT_ID);
        },
        detach: function (sessionId) {
          return ST2_api.detachClient(wid, sessionId, ST_CT_CLIENT_ID);
        },
      },
    });
  }, [wid]);

  // Test / introspection surface, mirroring use-workspace-tap.js's
  // window.__wtapHubCount. Never read by production code.
  React.useEffect(function () {
    window.__clientToolsExecutor = executor;
    return function () { window.__clientToolsExecutor = null; };
  }, [executor]);

  // Mount/unmount of the open session drives attach/heartbeat/detach.
  React.useEffect(function () {
    if (!wid || !sid) return undefined;
    var live = true;
    executor.start(sid).catch(function () { /* best-effort */ });
    var timer = setInterval(function () {
      if (!live) return;
      executor.heartbeat(sid).catch(function () { /* best-effort */ });
    }, ST_CT_HEARTBEAT_MS);
    return function () {
      live = false;
      clearInterval(timer);
      executor.stop(sid).catch(function () { /* best-effort */ });
    };
  }, [wid, sid, executor]);

  window.useWorkspaceTapListener(wid, function (ev) {
    executor.handleEvent(ev);
  });

  return null;
}

window.ST_ClientTools = ST_ClientTools;
