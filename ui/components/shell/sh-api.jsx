// Fresh shell - the ONLY file that names a URL (same discipline as the
// classic studio's st-api.jsx:1-5). Bodies are exactly what the shipped
// handlers accept. Every read takes an AbortSignal so useResource can
// cancel it.

var SH_api = {
  // ---- reads --------------------------------------------------------------
  workspaces: function (signal) {
    return window.primerApi.apiFetch("GET", "/workspaces?limit=200", null,
      { signal: signal });
  },

  sessions: function (wid, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/sessions?limit=200",
      null, { signal: signal });
  },

  // response_model=WorkspaceSession (primer/api/routers/sessions.py:643-656):
  // a FLAT row. binding, binding_epoch, turn_status and parked_status are
  // top-level siblings, there is no `session` sub-object, and the bound
  // agent is binding.agent_id (the row has no agent_id of its own). The S1
  // M14 task adds the unrealized PendingSessionMessage rows as one more
  // sibling, `pending_messages`, each carrying `parts` and never `content`.
  session: function (sid, signal) {
    return window.primerApi.apiFetch(
      "GET", "/sessions/" + encodeURIComponent(sid), null, { signal: signal });
  },

  messages: function (sid, limit, beforeSeq, signal) {
    var q = "?limit=" + encodeURIComponent(limit || 200);
    if (beforeSeq != null) q += "&before_seq=" + encodeURIComponent(beforeSeq);
    return window.primerApi.apiFetch(
      "GET", "/sessions/" + encodeURIComponent(sid) + "/messages" + q,
      null, { signal: signal });
  },

  filesTree: function (wid, path, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/files/tree?path="
        + encodeURIComponent(path || "."),
      null, { signal: signal });
  },

  fileRead: function (wid, path, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/files/read?path="
        + encodeURIComponent(path),
      null, { signal: signal });
  },

  // PUT /v1/workspaces/{wid}/files?path= with {content, encoding}
  // (primer/api/routers/workspaces.py:1511-1530; body FileWriteBody at
  // :161-177). 204 on success, so there is no response body to read.
  fileWrite: function (wid, path, content) {
    return window.primerApi.apiFetch(
      "PUT", "/workspaces/" + encodeURIComponent(wid) + "/files?path="
        + encodeURIComponent(path),
      { content: content, encoding: "text" });
  },

  collections: function (signal) {
    return window.primerApi.apiFetch("GET", "/collections?limit=200", null,
      { signal: signal });
  },

  // S2's document-by-path read (ui/components/knowledge.jsx:657-660).
  collectionDocument: function (cid, path, signal) {
    return window.primerApi.apiFetch(
      "GET", "/collections/" + encodeURIComponent(cid) + "/documents?path="
        + encodeURIComponent(path),
      null, { signal: signal });
  },

  commitLog: function (wid, limit, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/log?limit="
        + encodeURIComponent(limit || 100) + "&with_files=1",
      null, { signal: signal });
  },

  commit: function (wid, sha, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/commit/"
        + encodeURIComponent(sha),
      null, { signal: signal });
  },

  pendingYields: function (wid, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/yields/pending",
      null, { signal: signal });
  },

  sessionPendingYields: function (wid, sid, signal) {
    return window.primerApi.apiFetch(
      "GET", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/yields/pending",
      null, { signal: signal });
  },

  approvalRecords: function (signal) {
    return window.primerApi.apiFetch(
      "GET", "/tool_approval/records?status=all&offset=0&length=200",
      null, { signal: signal });
  },

  // S7 spec section 5. The Trace tab derives its tree from this read.
  timeline: function (sid, turnNo, signal) {
    return window.primerApi.apiFetch(
      "GET", "/sessions/" + encodeURIComponent(sid) + "/turns/"
        + encodeURIComponent(turnNo) + "/timeline",
      null, { signal: signal });
  },

  // ---- writes -------------------------------------------------------------
  createSession: function (wid, body) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions",
      body || {});
  },

  steer: function (wid, sid, content) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/steer",
      { content: content });
  },

  interrupt: function (wid, sid) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/interrupt", {});
  },

  // Pause parks a running session without discarding it; resume is the
  // idempotent start-or-resume the backend has always served. Interrupt
  // is the hard stop -- these two are the soft pair, and both endpoints
  // are live, so the shell has to reach them.
  pause: function (wid, sid) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/pause", {});
  },

  resume: function (wid, sid) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/resume", {});
  },

  // Interrupt stops the running turn and leaves the session alive.
  // Cancel ends the session. Restart brings an ended one back. Three
  // different endpoints doing three different things, all live.
  cancel: function (wid, sid) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/cancel", {});
  },

  restart: function (wid, sid) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/restart", {});
  },

  // S3 attach lifecycle. POST both attaches and heartbeats; DELETE
  // detaches. One URL builder, so the two can never drift.
  attach: function (wid, sid, clientId) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/attach", { client_id: clientId });
  },

  detach: function (wid, sid, clientId) {
    return window.primerApi.apiFetch(
      "DELETE", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/attach", { client_id: clientId });
  },

  // S1 spec section 6: {kind, agent_id | graph_id, profile_id?}.
  switchBinding: function (wid, sid, binding) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/binding", binding);
  },

  // S1 port of the chat rewind (primer/api/routers/chats.py:1072-1110,
  // body {seq} at :1047-1058). Conversation-only: workspace files are
  // git-committed already and scoped restore is a programme follow-up.
  rewind: function (wid, sid, seq) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/rewind", { seq: seq });
  },

  // S1 plan pinned decision 14. 409 when a turn is open or the session is
  // parked, which the caller surfaces as a toast, never a modal.
  compact: function (wid, sid) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/compact", {});
  },

  approve: function (sid, tcid) {
    return window.primerApi.apiFetch(
      "POST", "/sessions/" + encodeURIComponent(sid) + "/tool_approval/respond",
      { tool_call_id: tcid, decision: "approved" });
  },

  reject: function (sid, tcid, reason) {
    return window.primerApi.apiFetch(
      "POST", "/sessions/" + encodeURIComponent(sid) + "/tool_approval/respond",
      { tool_call_id: tcid, decision: "rejected", reason: reason || "" });
  },

  answer: function (sid, tcid, response) {
    return window.primerApi.apiFetch(
      "POST", "/sessions/" + encodeURIComponent(sid) + "/ask_user/respond",
      { tool_call_id: tcid, response: response });
  },

  // ---- cache keys ---------------------------------------------------------
  keys: {
    sessions: function (wid) { return "shell-sessions:" + wid; },
    session: function (sid) { return "shell-session:" + sid; },
    pending: function (wid) { return "shell-pending:" + wid; },
    sessionPending: function (sid) { return "shell-session-pending:" + sid; },
    tree: function (wid, path) { return "shell-tree:" + wid + ":" + (path || "."); },
    file: function (wid, path) { return "shell-file:" + wid + ":" + path; },
    collections: function () { return "shell-collections"; },
    document: function (cid, path) { return "shell-document:" + cid + ":" + path; },
    log: function (wid) { return "shell-log:" + wid; },
    commit: function (wid, sha) { return "shell-commit:" + wid + ":" + sha; },
    records: function () { return "shell-approval-records"; },
    timeline: function (sid, turnNo) {
      return "shell-timeline:" + sid + ":" + turnNo;
    },
  },
};

window.SH_api = SH_api;
