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

  // Cross-workspace (revamp section 3): the rail and palette reach every
  // session. The top-level route's rows differ from the workspace-scoped
  // ones (id vs session_id; last_turn_at vs last_activity_at), so this
  // seam NORMALIZES to the workspace-scoped shape every consumer already
  // reads - the difference must not leak past this file.
  // Sessions NEWEST-FIRST via /sessions/find (BDD pass 2026-08-24):
  // the plain GET list is id-ordered, so past 200 rows the cap
  // silently dropped the NEWEST sessions - the one you just created
  // vanished from its own sidebar on any busy install. Ordering desc
  // makes the cap shed the oldest instead, and an optional wid pushes
  // the workspace filter server-side for the sidebar's feed.
  allSessions: function (signal, wid) {
    var body = {
      predicate: wid ? {
        kind: "predicate",
        left: { kind: "field", name: "workspace_id" },
        op: "=",
        right: { kind: "value", value: wid },
      } : null,
      page: { kind: "offset", offset: 0, length: 200 },
      order_by: [{ field: "created_at", direction: "desc" }],
    };
    return window.primerApi.apiFetch(
      "POST", "/sessions/find", body, { signal: signal }
    ).then(function (out) {
      var items = ((out && out.items) || []).map(function (r) {
        return Object.assign({}, r, {
          session_id: r.session_id || r.id,
          last_activity_at: r.last_activity_at || r.last_turn_at
            || r.created_at,
        });
      });
      return { items: items };
    });
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

  // `tail=1` is the crux: the /messages endpoint returns the FIRST `limit`
  // records by default (the OLDEST), so a transcript fetch showed the oldest
  // turns and any session past ~28 turns looked frozen. `tail=1` returns the
  // LAST `limit` (newest). `beforeSeq` is kept for the one caller's signature
  // but ignored: the endpoint has no before_seq (it takes tail/after_seq/
  // offset), so the old branch was a no-op.
  messages: function (sid, limit, beforeSeq, signal) {
    var q = "?limit=" + encodeURIComponent(limit || 200) + "&tail=1";
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

  // PUT /v1/workspaces/{wid}/files?path= with {content, encoding}.
  // 204 on success. `etag` (from a prior read) makes the write
  // conditional: the route answers 412 when the file changed on disk,
  // which is the file doc's changed-on-disk banner (revamp section 6).
  fileWrite: function (wid, path, content, etag) {
    var url = "/workspaces/" + encodeURIComponent(wid) + "/files?path="
      + encodeURIComponent(path);
    if (etag) url += "&etag=" + encodeURIComponent(etag);
    return window.primerApi.apiFetch(
      "PUT", url, { content: content, encoding: "text" });
  },

  // Per-workspace lifecycle-event streaming opt-in (workspaces.py
  // set_workspace_events; body wraps the config so null clears).
  setWorkspaceEvents: function (wid, enabled) {
    return window.primerApi.apiFetch(
      "PUT", "/workspaces/" + encodeURIComponent(wid) + "/events",
      { config: enabled ? { enabled: true } : null });
  },

  // The platform event log window (events.py list_events; admin-gated).
  events: function (opts, signal) {
    var o = opts || {};
    var q = ["limit=" + encodeURIComponent(o.limit || 100)];
    if (o.afterId != null) q.push("after_id=" + encodeURIComponent(o.afterId));
    if (o.eventType) q.push("event_type=" + encodeURIComponent(o.eventType));
    if (o.entityKind) q.push("entity_kind=" + encodeURIComponent(o.entityKind));
    if (o.workspaceId) q.push("workspace_id=" + encodeURIComponent(o.workspaceId));
    return window.primerApi.apiFetch(
      "GET", "/events?" + q.join("&"), null, { signal: signal });
  },

  // PATCH rename (workspaces.py rename_session): {name}; null clears.
  renameSession: function (wid, sid, name) {
    return window.primerApi.apiFetch(
      "PATCH", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid),
      { name: name });
  },

  // ---- file management (revamp section 6; routes pre-existing) -----------
  fileDelete: function (wid, path) {
    return window.primerApi.apiFetch(
      "DELETE", "/workspaces/" + encodeURIComponent(wid) + "/files?path="
        + encodeURIComponent(path));
  },
  makeDir: function (wid, path) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/files/dir?path="
        + encodeURIComponent(path));
  },
  fileMove: function (wid, src, dst) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/files/move?src="
        + encodeURIComponent(src) + "&dst=" + encodeURIComponent(dst));
  },
  // Dropped File objects arrive as base64 so binaries survive.
  fileUpload: function (wid, path, base64Content) {
    return window.primerApi.apiFetch(
      "PUT", "/workspaces/" + encodeURIComponent(wid) + "/files?path="
        + encodeURIComponent(path),
      { content: base64Content, encoding: "base64" });
  },
  // Raw-bytes download rides the browser, not apiFetch: an <a href>.
  fileDownloadUrl: function (wid, path) {
    return "/v1/workspaces/" + encodeURIComponent(wid)
      + "/files/download?path=" + encodeURIComponent(path);
  },

  collections: function (signal) {
    return window.primerApi.apiFetch("GET", "/collections?limit=200", null,
      { signal: signal });
  },

  // Entity lists for the palette's mixed results (wiring plan P1 T5).
  agents: function (signal) {
    return window.primerApi.apiFetch("GET", "/agents?limit=200", null,
      { signal: signal });
  },
  graphs: function (signal) {
    return window.primerApi.apiFetch("GET", "/graphs?limit=200", null,
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

  // The field is `instruction`. SteerBody names it that because the one
  // endpoint covers invoking, steering and resuming, and "content" was
  // not a field at all: every send from the composer 422'd, which is the
  // most important action in the console failing on every use.
  steer: function (wid, sid, instruction) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/steer",
      { instruction: instruction });
  },

  deleteSession: function (wid, sid) {
    return window.primerApi.apiFetch(
      "DELETE", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid));
  },

  // Drops one queued follow-up steer (the transcript's "queued" chip)
  // before a turn realizes it. The session doc has called this since
  // the flag day; the function (and its route) did not exist, so the
  // chip's X threw and a queued steer was undismissable.
  dismissQueuedSteer: function (wid, sid, pendingId) {
    return window.primerApi.apiFetch(
      "DELETE", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/pending_messages/"
        + encodeURIComponent(pendingId));
  },

  interrupt: function (wid, sid) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/interrupt", {});
  },

  // The two session controls the composer cannot express. Sending a
  // message already covers the rest: POST .../steer invokes a CREATED
  // session, steers a running one, RESUMES a paused one and reopens an
  // ENDED one, which is why S8 gave the shell one send and one verb.
  // Nothing you can type parks a session or ends it, so these two need
  // a way in of their own.
  pause: function (wid, sid) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/pause", {});
  },

  cancel: function (wid, sid) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/cancel", {});
  },

  // S3 attach lifecycle. POST both attaches and heartbeats; DELETE
  // detaches. One URL builder, so the two can never drift.
  attach: function (wid, sid, clientId) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/attach", { client_id: clientId });
  },

  // client_id is a QUERY parameter on the detach route, not a body.
  // Sent as a body it never arrived, so every detach 422'd on a missing
  // required parameter and an attachment could only ever lapse by TTL.
  detach: function (wid, sid, clientId) {
    return window.primerApi.apiFetch(
      "DELETE", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/attach?client_id="
        + encodeURIComponent(clientId), null);
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
  // RewindBody names it `to_seq`, not `seq`: rewinding sent an unknown
  // field and the call 422'd every time.
  rewind: function (wid, sid, toSeq) {
    return window.primerApi.apiFetch(
      "POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions/"
        + encodeURIComponent(sid) + "/rewind", { to_seq: toSeq });
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
    allSessions: function () { return "shell-all-sessions"; },
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
