/* global React */
// use-workspace-tap.js — ONE shared EventSource per workspace, fanned out to
// every consumer in a Studio view.
//
// PROBLEM (fe-review N4 "triple SSE per Studio view"): the right rail opened
// two EventSources to the same GET /v1/workspaces/{wid}/tap — one in
// ActionRequired (reconcile pending yields) and one in WorkspaceTap (the
// activity feed) — and the graph run-view opened a third just to trigger a
// node-state refetch. Same URL, same workspace, N connections.
//
// FIX: a module-level hub, keyed by workspace id, owns exactly ONE EventSource
// and multiplexes its frames to all subscribers. The hub is ref-counted: it
// opens on the first subscriber and closes + is discarded on the last. It is
// deliberately a plain module singleton (not React context) so it works
// identically inside the Studio and on the standalone /workspaces page, and so
// no provider has to be threaded through studio.jsx.
//
// Two consumption hooks:
//   • useWorkspaceTap(wid)         → { events, connState, clear }. Re-renders
//     the caller when a frame arrives / the connection state changes. Used by
//     the activity feed (WorkspaceTap) which renders the rolling buffer.
//   • useWorkspaceTapListener(wid, onEvent) → fires onEvent(ev) per frame
//     WITHOUT re-rendering the caller. Used by ActionRequired (refetch pending
//     on yielded/done) and SD_GraphRunView (refetch node states on
//     graph_transition/done/error). onEvent is held in a ref so an inline
//     closure does not churn the subscription.
//
// No-build scope rules: top-level declarations use `var`; helpers are prefixed
// WTAP_ to avoid global collisions; exported symbols are written to window.X.
//
// This mirrors the live-from-connect contract of the tap endpoint: with no
// cursor the server starts each in-scope session at its high-water mark, so
// history is NOT replayed — the shared buffer only accrues events from connect
// onward. Session-scoped views that need a gap-free history seam (cursored
// replay) still open their own tap; those are genuinely session-scoped and are
// not part of this workspace-wide fan-out.

// Rolling buffer cap — matches WorkspaceTap's prior WTP_MAX_EVENTS.
var WTAP_MAX_EVENTS = 500;

// wid -> hub. A hub = { es, refs, events, connState, renderSubs, eventSubs }.
var WTAP_HUBS = {};

function WTAP_getHub(wid) {
  var hub = WTAP_HUBS[wid];
  if (hub) return hub;
  hub = {
    wid: wid,
    es: null,
    refs: 0,
    events: [],
    connState: "connecting",
    renderSubs: [], // fn(hub) — buffer/connState changed
    eventSubs: [],  // fn(ev)  — one incoming frame
  };
  WTAP_HUBS[wid] = hub;
  return hub;
}

function WTAP_notifyRender(hub) {
  var subs = hub.renderSubs.slice();
  for (var i = 0; i < subs.length; i++) {
    try { subs[i](hub); } catch (_e) { /* no-op */ }
  }
}

function WTAP_open(hub) {
  if (hub.es || !hub.wid) return;
  var url = WTAP_cursorUrl(hub);
  var es;
  try {
    es = new EventSource(url, { withCredentials: true });
  } catch (_e) {
    hub.connState = "error";
    WTAP_notifyRender(hub);
    WTAP_notifyStoresConnState(hub);
    return;
  }
  hub.es = es;
  hub.connState = "connecting";
  WTAP_notifyStoresConnState(hub);
  es.onopen = function () {
    hub.connState = "live";
    WTAP_notifyRender(hub);
    WTAP_notifyStoresConnState(hub);
    // On (re)connect, re-read the REST tail for every observed store so a tap
    // that attached late backfills the gap instead of waiting for a page refresh.
    WTAP_catchUpStores(hub);
  };
  es.onmessage = function (e) {
    var ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    if (!ev || typeof ev !== "object") return;
    var next = hub.events.concat(ev);
    if (next.length > WTAP_MAX_EVENTS) next = next.slice(next.length - WTAP_MAX_EVENTS);
    hub.events = next;
    // Per-frame listeners first (reconcile / refetch triggers), then renderers.
    var esubs = hub.eventSubs.slice();
    for (var i = 0; i < esubs.length; i++) {
      try { esubs[i](ev); } catch (_e2) { /* no-op */ }
    }
    // Phase 2: also route the frame to the matching session store (additive;
    // the render/event notify above is unchanged).
    WTAP_routeToStores(hub, ev);
    WTAP_notifyRender(hub);
  };
  es.onerror = function () {
    // EventSource auto-reconnects natively via Last-Event-ID; reflect the drop.
    hub.connState = "error";
    WTAP_notifyRender(hub);
    WTAP_notifyStoresConnState(hub);
  };
}

function WTAP_close(hub) {
  if (hub.es) {
    try { hub.es.close(); } catch (_e) { /* no-op */ }
    hub.es = null;
  }
}

// --- Phase 2 additive: route session-scoped frames to the conversation store
// (spec section 4). These helpers are strictly additive: the existing render /
// event subscriber notify path below is untouched, so the sidebar and status
// consumers see byte-for-byte the same frames. A frame is routed to a store
// only if a store already exists for (wid, sid) in the store registry - the
// hub never creates a store (that is the component's job via getStore), so a
// session nobody is watching produces no empty store.

function WTAP_obsPrefix(wid) {
  return "session-store:" + wid + ":";
}

// Route one frame to the store for its session_id, if a store exists.
function WTAP_routeToStores(hub, ev) {
  if (!window.SS_STORES || !window.SS_key || !window.SS_apply) return;
  var sid = ev.session_id;
  if (sid == null) return;
  var store = window.SS_STORES[window.SS_key(hub.wid, sid)];
  if (store) {
    try { window.SS_apply(store, ev); } catch (_e) { /* no-op */ }
  }
}

// Surface the hub's connState to every store for this wid (C5: one source).
function WTAP_notifyStoresConnState(hub) {
  if (!window.SS_STORES || !window.SS_setConnState) return;
  var prefix = WTAP_obsPrefix(hub.wid);
  for (var key in window.SS_STORES) {
    if (Object.prototype.hasOwnProperty.call(window.SS_STORES, key)
        && key.indexOf(prefix) === 0) {
      var store = window.SS_STORES[key];
      try { window.SS_setConnState(store, hub.connState); } catch (_e) { /* no-op */ }
    }
  }
}

// Trigger a REST catch-up for every store for this wid (spec section 4.1).
function WTAP_catchUpStores(hub) {
  if (!window.SS_STORES || !window.SS_catchUp) return;
  var prefix = WTAP_obsPrefix(hub.wid);
  for (var key in window.SS_STORES) {
    if (Object.prototype.hasOwnProperty.call(window.SS_STORES, key)
        && key.indexOf(prefix) === 0) {
      var store = window.SS_STORES[key];
      try { window.SS_catchUp(store); } catch (_e) { /* no-op */ }
    }
  }
}

// Open the EventSource with a cursor built from the observed stores' high-
// water, so the tap resumes where a store's REST seed left off (the gap-free
// seam). Bare URL when no store is observed, so existing consumers keep the
// live-from-connect behaviour byte-for-byte.
function WTAP_cursorUrl(hub) {
  var url = "/v1/workspaces/" + encodeURIComponent(hub.wid) + "/tap";
  if (!window.SS_STORES || !window.SS_highWater) return url;
  var seqs = {};
  var prefix = WTAP_obsPrefix(hub.wid);
  for (var key in window.SS_STORES) {
    if (Object.prototype.hasOwnProperty.call(window.SS_STORES, key)
        && key.indexOf(prefix) === 0) {
      var store = window.SS_STORES[key];
      var hw = window.SS_highWater(store);
      if (hw > 0) seqs[store.sid] = hw;
    }
  }
  if (Object.keys(seqs).length === 0) return url;
  var payload = { known_as_of: "1970-01-01T00:00:00+00:00", seqs: seqs };
  var b64;
  try {
    b64 = btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
  } catch (_e) {
    return url;
  }
  var token = b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return url + "?cursor=" + encodeURIComponent(token);
}

function WTAP_retain(wid) {
  var hub = WTAP_getHub(wid);
  hub.refs += 1;
  if (hub.refs === 1) WTAP_open(hub);
  return hub;
}

function WTAP_release(wid) {
  var hub = WTAP_HUBS[wid];
  if (!hub) return;
  hub.refs -= 1;
  if (hub.refs <= 0) {
    WTAP_close(hub);
    delete WTAP_HUBS[wid];
  }
}

function WTAP_clear(wid) {
  var hub = WTAP_HUBS[wid];
  if (!hub) return;
  hub.events = [];
  WTAP_notifyRender(hub);
}

// ---------------------------------------------------------------------------
// Renderer hook — re-renders the caller on buffer / connection changes.
// ---------------------------------------------------------------------------

function useWorkspaceTap(wid) {
  var setTick = React.useState(0)[1];
  var hubRef = React.useRef(null);

  React.useEffect(function () {
    if (!wid) { hubRef.current = null; return undefined; }
    var hub = WTAP_retain(wid);
    hubRef.current = hub;
    var sub = function () { setTick(function (n) { return (n + 1) & 0x3fffffff; }); };
    hub.renderSubs.push(sub);
    // Sync to whatever the shared buffer already holds (a late subscriber must
    // not render an empty list while the hub is mid-stream).
    sub();
    return function () {
      var idx = hub.renderSubs.indexOf(sub);
      if (idx >= 0) hub.renderSubs.splice(idx, 1);
      hubRef.current = null;
      WTAP_release(wid);
    };
  }, [wid]);

  var hub = hubRef.current || (wid ? WTAP_HUBS[wid] : null);
  return {
    events: hub ? hub.events : [],
    connState: hub ? hub.connState : "connecting",
    clear: function () { if (wid) WTAP_clear(wid); },
  };
}

// ---------------------------------------------------------------------------
// Listener hook — fires onEvent(ev) per frame, no re-render of the caller.
// ---------------------------------------------------------------------------

function useWorkspaceTapListener(wid, onEvent) {
  var cbRef = React.useRef(onEvent);
  cbRef.current = onEvent;

  React.useEffect(function () {
    if (!wid) return undefined;
    var hub = WTAP_retain(wid);
    var sub = function (ev) {
      var f = cbRef.current;
      if (typeof f === "function") f(ev);
    };
    hub.eventSubs.push(sub);
    return function () {
      var idx = hub.eventSubs.indexOf(sub);
      if (idx >= 0) hub.eventSubs.splice(idx, 1);
      WTAP_release(wid);
    };
  }, [wid]);
}

// ---------------------------------------------------------------------------
// No-build window exports
// ---------------------------------------------------------------------------
window.useWorkspaceTap = useWorkspaceTap;
window.useWorkspaceTapListener = useWorkspaceTapListener;
// Test/introspection surface — how many live hubs (== EventSources) exist.
window.__wtapHubCount = function () { return Object.keys(WTAP_HUBS).length; };
// Current hub connState for a workspace, or null when no hub exists. Lets a
// store created AFTER the hub's onopen seed its connState correctly - the
// open/error fan-out only reaches stores that exist at that instant, so
// without this seed a late store sits at "connecting" forever and the
// history-poll demotion (historyLive) never engages.
window.WTAP_hubConnState = function (wid) {
  var hub = wid ? WTAP_HUBS[wid] : null;
  return hub ? hub.connState : null;
};
