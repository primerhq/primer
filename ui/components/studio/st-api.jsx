// Studio revamp - the only file that names a URL (ui/studio/STUDIO-WIRING.md §2.3).
//
// Bodies are exactly what the shipped endpoints accept; do not "improve" them.
// Every read takes an optional AbortSignal so useResource can cancel it.

function ST2_attachUrl(wid, sid) {
  return (
    "/workspaces/" + encodeURIComponent(wid) +
    "/sessions/" + encodeURIComponent(sid) + "/attach"
  );
}

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
  // The turn trail. `with_files=1` adds per-file {path, additions, deletions,
  // binary}; without it every commit's `files` is null. Deliberately unpolled
  // by the caller (WIRING §7.1 keeps WS_LogTab's manual-refresh behaviour) -
  // history does not change under you, and a poll here would refetch the whole
  // page on a timer for no reason.
  trail: function (wid, limit, withFiles, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/log?limit="
        + encodeURIComponent(limit) + (withFiles ? "&with_files=1" : ""),
      null, { signal: signal });
  },

  // One commit's per-file unified patches. Fetched only when a reader opens a
  // row, which is why the trail carries counts and not content.
  commit: function (wid, sha, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/commit/"
        + encodeURIComponent(sha),
      null, { signal: signal });
  },

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

  // ---- client-tool attachment (S3) ------------------------------------------
  // One place the attach URL is built, so POST (attach + heartbeat) and
  // DELETE (detach) can never drift apart.
  // primer/api/routers/sessions.py (attach endpoint)
  attachClient: function (wid, sid, clientId) {
    return window.primerApi.apiFetch(
      "POST", ST2_attachUrl(wid, sid), { client_id: clientId });
  },

  detachClient: function (wid, sid, clientId) {
    return window.primerApi.apiFetch(
      "DELETE",
      ST2_attachUrl(wid, sid) + "?client_id=" + encodeURIComponent(clientId));
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
    // Same key shape WS_LogTab uses, so the two views share one cache entry
    // when their limits agree. with_files varies the key: the two responses
    // differ in shape, so they must not alias.
    trail: function (wid, limit, withFiles) {
      return "workspace-log:" + wid + ":" + limit + (withFiles ? ":files" : "");
    },
    commit: function (wid, sha) { return "workspace-commit:" + wid + ":" + sha; },
  },
};

window.ST2_api = ST2_api;
