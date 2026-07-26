// Studio revamp - the only file that names a URL (ui/studio/STUDIO-WIRING.md §2.3).
//
// Bodies are exactly what the shipped endpoints accept; do not "improve" them.
// Every read takes an optional AbortSignal so useResource can cancel it.

var ST2_api = {
  // ---- reads ---------------------------------------------------------------

  // Workspace-wide pending yields - what the attention bar counts.
  // primer/api/routers/workspaces.py:1564
  pendingYields: function (wid, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/yields/pending",
      null, { signal: signal });
  },

  // Pending yields for one session - what the inline transcript card reads.
  // primer/api/routers/workspaces.py:1646
  sessionPendingYields: function (wid, sid, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/yields/pending",
      null, { signal: signal });
  },

  sessions: function (wid, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/sessions?limit=200",
      null, { signal: signal });
  },

  // The .state git trail. Deliberately unpolled (WS_LogTab is manual-refresh).
  // NOTE: CommitInfo carries sha/subject/committed_at + the X-Primer-* trailers
  // only - there is NO per-commit file list or +n/-m stat, so a Changes view
  // cannot enumerate changed paths from this alone. See the plan's §0.2.
  workspaceLog: function (wid, limit, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/log?limit="
        + (limit || 100),
      null, { signal: signal });
  },

  // Graph run node states. This is the TOP-LEVEL compute route
  // (primer/api/routers/compute.py:249), not a workspace-scoped one - for a
  // graph-bound session the run id IS the session id.
  nodeStates: function (graphId, runId, signal) {
    return window.primerApi.apiFetch(
      "GET", "/graphs/" + encodeURIComponent(graphId) + "/runs/"
        + encodeURIComponent(runId) + "/node_states",
      null, { signal: signal });
  },

  // ---- yield writes (bodies unchanged from the shipped handlers) -----------

  approve: function (sid, tcid) {
    return window.primerApi.apiFetch(
      "POST", "/sessions/" + encodeURIComponent(sid) + "/tool_approval/respond",
      { tool_call_id: tcid, decision: "approved" });
  },

  deny: function (sid, tcid, reason) {
    return window.primerApi.apiFetch(
      "POST", "/sessions/" + encodeURIComponent(sid) + "/tool_approval/respond",
      { tool_call_id: tcid, decision: "rejected", reason: reason || "" });
  },

  answer: function (sid, tcid, response) {
    return window.primerApi.apiFetch(
      "POST", "/sessions/" + encodeURIComponent(sid) + "/ask_user/respond",
      { tool_call_id: tcid, response: response });
  },

  cancelYield: function (sid, tcid, reason) {
    return window.primerApi.apiFetch(
      "POST", "/sessions/" + encodeURIComponent(sid) + "/yields/"
        + encodeURIComponent(tcid) + "/cancel",
      { reason: reason || "operator cancelled" });
  },

  // ---- cache keys ---------------------------------------------------------
  // One place so a mutation's `invalidates` can never drift from the resource
  // key the reader registered.
  keys: {
    pending: function (wid) { return "studio-yields-pending:" + wid; },
    sessionPending: function (sid) { return "session-adapter:pending:" + sid; },
    sessions: function (wid) { return "studio-sessions:" + wid; },
    log: function (wid) { return "studio-log:" + wid; },
    nodeStates: function (gid, rid) { return "graph-node-states:" + gid + ":" + rid; },
  },
};

window.ST2_api = ST2_api;
