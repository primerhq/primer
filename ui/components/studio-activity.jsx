/* global React, Icon, Btn */
// StudioActivity — right sidebar: Action Required + Workspace Activity feed.
// PR-B / B4. Replaces the region-activity placeholder in studio.jsx.
//
// Components exported:
//   window.StudioActivity  — outer shell; props: { wid, studio }
//   window.ActionRequired  — pending-yields list with inline actions
//   window.WorkspaceActivity — header wrapper around window.WorkspaceTap
//
// No-build scope rules (see workspace-tap.jsx):
//   • top-level declarations use `var` (not const/let)
//   • helpers are prefixed SA_ to avoid global collisions
//   • every exported symbol is written to window.X = X
//
// Live-reconcile strategy (simplest correct):
//   ActionRequired opens a lightweight EventSource to the workspace tap and
//   calls refetch() on the /yields/pending resource whenever a "yielded" or
//   "done" event arrives. A 300 ms debounce prevents burst re-fetches when
//   multiple events land at once. This reuses the existing tap infrastructure
//   (no extra connection object) and keeps the reconcile logic in < 20 lines.
//
// Endpoint shapes (verified in session-detail.jsx / approvals.jsx):
//   • Approve :  POST /sessions/{sid}/tool_approval/respond
//                body: { tool_call_id, decision: "approved" }
//   • Reject  :  POST /sessions/{sid}/tool_approval/respond
//                body: { tool_call_id, decision: "rejected", reason: "" }
//   • Respond :  POST /sessions/{sid}/ask_user/respond
//                body: { tool_call_id, response: <string> }
//   • Cancel  :  POST /sessions/{sid}/yields/{tcid}/cancel
//                body: { reason: "operator cancelled" }

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function SA_short(sid) {
  if (!sid) return "—";
  if (sid.length <= 20) return sid;
  return sid.slice(0, 10) + "…" + sid.slice(-6);
}

// ---------------------------------------------------------------------------
// ActionRequired
// ---------------------------------------------------------------------------

function ActionRequired({ wid, studio }) {
  var apiFetch = window.primerApi.apiFetch;
  var useResource = window.primerApi.useResource;

  // Snapshot of pending yields for the whole workspace.
  var pending = useResource(
    "studio-yields-pending:" + wid,
    function(signal) {
      return apiFetch("GET", "/workspaces/" + encodeURIComponent(wid) + "/yields/pending", null, { signal: signal });
    },
    { pollMs: 15000, deps: [wid] }
  );

  var items = (pending.data && Array.isArray(pending.data.items)) ? pending.data.items : [];

  // Live reconcile: subscribe to the workspace tap; refetch on yielded/done.
  // We keep this EventSource independent of WorkspaceTap so it is always
  // open regardless of the activity feed's filter state.
  var debounceRef = React.useRef(null);
  React.useEffect(function() {
    if (!wid) return;
    var url = "/v1/workspaces/" + encodeURIComponent(wid) + "/tap";
    var es;
    try {
      es = new EventSource(url, { withCredentials: true });
    } catch (_e) {
      return;
    }
    es.onmessage = function(e) {
      var ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      if (!ev || typeof ev !== "object") return;
      var cls = ev.class;
      if (cls === "yielded" || cls === "done") {
        clearTimeout(debounceRef.current);
        debounceRef.current = setTimeout(function() {
          pending.refetch();
        }, 300);
      }
    };
    return function() {
      clearTimeout(debounceRef.current);
      try { es.close(); } catch (_) { /* no-op */ }
    };
  }, [wid]); // eslint-disable-line react-hooks/exhaustive-deps

  // Per-item respond state: { [item_id]: { draft, submitting, error } }
  var [respondState, setRespondState] = React.useState({});

  function getRespond(id) {
    return respondState[id] || { draft: "", submitting: false, error: null };
  }

  function patchRespond(id, patch) {
    setRespondState(function(prev) {
      var cur = prev[id] || { draft: "", submitting: false, error: null };
      var next = {};
      for (var k in prev) next[k] = prev[k];
      next[id] = Object.assign({}, cur, patch);
      return next;
    });
  }

  // Optimistically remove an item after a successful action, then re-confirm
  // via the next reconcile refetch.
  var [hidden, setHidden] = React.useState({});
  function hide(id) {
    setHidden(function(prev) {
      var next = {};
      for (var k in prev) next[k] = prev[k];
      next[id] = true;
      return next;
    });
    setTimeout(function() { pending.refetch(); }, 800);
  }

  // Action handlers
  function handleApprove(item) {
    apiFetch(
      "POST",
      "/sessions/" + encodeURIComponent(item.session_id) + "/tool_approval/respond",
      { tool_call_id: item.tool_call_id, decision: "approved" }
    ).then(function() {
      hide(item.tool_call_id);
    }).catch(function(err) {
      // Surface error inline — stay visible so user can retry
      patchRespond(item.tool_call_id, { error: (err && (err.detail || err.title || err.message)) || "Approve failed" });
    });
  }

  function handleReject(item) {
    apiFetch(
      "POST",
      "/sessions/" + encodeURIComponent(item.session_id) + "/tool_approval/respond",
      { tool_call_id: item.tool_call_id, decision: "rejected", reason: "" }
    ).then(function() {
      hide(item.tool_call_id);
    }).catch(function(err) {
      patchRespond(item.tool_call_id, { error: (err && (err.detail || err.title || err.message)) || "Reject failed" });
    });
  }

  function handleRespondSubmit(item) {
    var rs = getRespond(item.tool_call_id);
    if (!rs.draft.trim()) return;
    patchRespond(item.tool_call_id, { submitting: true, error: null });
    apiFetch(
      "POST",
      "/sessions/" + encodeURIComponent(item.session_id) + "/ask_user/respond",
      { tool_call_id: item.tool_call_id, response: rs.draft.trim() }
    ).then(function() {
      hide(item.tool_call_id);
    }).catch(function(err) {
      patchRespond(item.tool_call_id, { submitting: false, error: (err && (err.detail || err.title || err.message)) || "Respond failed" });
    });
  }

  function handleRespondKeyDown(item, e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleRespondSubmit(item);
    }
  }

  function handleCancel(item) {
    apiFetch(
      "POST",
      "/sessions/" + encodeURIComponent(item.session_id) + "/yields/" + encodeURIComponent(item.tool_call_id) + "/cancel",
      { reason: "operator cancelled" }
    ).then(function() {
      hide(item.tool_call_id);
    }).catch(function(err) {
      patchRespond(item.tool_call_id, { error: (err && (err.detail || err.title || err.message)) || "Cancel failed" });
    });
  }

  function handleFocusSession(sessionId) {
    if (!studio || !studio.openTab) return;
    studio.openTab({
      id: "session:" + sessionId,
      kind: "session",
      ref: sessionId,
      title: sessionId,
    });
  }

  var visibleItems = items.filter(function(it) { return !hidden[it.tool_call_id]; });
  var count = visibleItems.length;

  return (
    <div
      className="st-section"
      style={{
        borderBottom: "1px solid var(--border)",
        display: "flex",
        flexDirection: "column",
        flexShrink: 0,
        maxHeight: count > 0 ? 320 : 60,
        overflow: "hidden",
      }}
      data-testid="action-required"
    >
      {/* Section header */}
      <div
        style={{
          height: 34,
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "0 12px",
          borderBottom: count > 0 ? "1px solid var(--border)" : "none",
        }}
      >
        <span style={{ color: "var(--amber)", fontSize: 13 }}>⚠</span>
        <span
          style={{
            fontSize: 10.5,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            fontWeight: 600,
            color: "var(--text-2)",
            flex: 1,
          }}
        >
          Action Required
        </span>
        {count > 0 && (
          <span
            data-testid="action-required-count"
            style={{
              background: "var(--amber-dim)",
              color: "var(--amber)",
              borderRadius: 999,
              padding: "1px 7px",
              fontSize: 10.5,
              fontWeight: 700,
              fontFamily: "IBM Plex Mono, monospace",
            }}
          >
            {count}
          </span>
        )}
        {pending.loading && !pending.data && (
          <span style={{ color: "var(--text-4)", fontSize: 10.5 }}>…</span>
        )}
      </div>

      {/* Item list */}
      <div
        data-testid="action-required-list"
        style={{ overflowY: "auto", flex: 1 }}
      >
        {count === 0 && !pending.loading && (
          <div
            style={{
              padding: "14px 12px",
              fontSize: 12,
              color: "var(--text-4)",
              textAlign: "center",
              lineHeight: 1.5,
            }}
          >
            No pending actions. Everything's running clean.
          </div>
        )}

        {visibleItems.map(function(item) {
          var rs = getRespond(item.tool_call_id);
          var isApproval = item.kind === "approval";
          var isAsk = item.kind === "ask_user";
          var isCancelable = item.kind === "watch_files" || item.kind === "sleep";
          var sidShort = SA_short(item.session_id);

          return (
            <div
              key={item.tool_call_id}
              data-testid="action-item"
              style={{
                padding: "10px 12px",
                borderBottom: "1px solid var(--border)",
                display: "flex",
                flexDirection: "column",
                gap: 6,
              }}
            >
              {/* Session label + kind */}
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <button
                  type="button"
                  data-testid="action-session-link"
                  onClick={function() { handleFocusSession(item.session_id); }}
                  style={{
                    background: "none",
                    border: "none",
                    padding: 0,
                    cursor: "pointer",
                    color: "var(--accent)",
                    fontFamily: "IBM Plex Mono, monospace",
                    fontSize: 11,
                    textDecoration: "underline",
                    textUnderlineOffset: 2,
                  }}
                  title={item.session_id}
                >
                  {sidShort}
                </button>
                <span
                  style={{
                    color: "var(--amber)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    letterSpacing: "0.05em",
                    fontWeight: 600,
                  }}
                >
                  {item.kind}
                </span>
              </div>

              {/* Prompt text */}
              {item.prompt && (
                <div
                  style={{
                    fontSize: 12,
                    lineHeight: 1.5,
                    color: "var(--text-2)",
                    wordBreak: "break-word",
                    whiteSpace: "pre-wrap",
                    maxHeight: 72,
                    overflowY: "auto",
                  }}
                >
                  {item.prompt}
                </div>
              )}

              {/* Inline error */}
              {rs.error && (
                <div style={{ color: "var(--red)", fontSize: 11 }}>{rs.error}</div>
              )}

              {/* Approval actions */}
              {isApproval && (
                <div
                  data-testid="action-approval-controls"
                  style={{ display: "flex", gap: 6 }}
                >
                  <button
                    type="button"
                    data-testid="approve"
                    onClick={function() { handleApprove(item); }}
                    style={{
                      flex: 1,
                      padding: "4px 0",
                      borderRadius: 6,
                      border: "1px solid oklch(0.82 0.18 145 / 0.4)",
                      background: "var(--green-dim)",
                      color: "var(--green)",
                      cursor: "pointer",
                      fontSize: 12,
                      fontWeight: 600,
                    }}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    data-testid="reject"
                    onClick={function() { handleReject(item); }}
                    style={{
                      flex: 1,
                      padding: "4px 0",
                      borderRadius: 6,
                      border: "1px solid oklch(0.75 0.18 25 / 0.4)",
                      background: "var(--red-dim)",
                      color: "var(--red)",
                      cursor: "pointer",
                      fontSize: 12,
                      fontWeight: 600,
                    }}
                  >
                    Reject
                  </button>
                </div>
              )}

              {/* Ask-user respond */}
              {isAsk && (
                <div
                  data-testid="action-ask-controls"
                  style={{ display: "flex", gap: 6 }}
                >
                  <input
                    type="text"
                    data-testid="respond"
                    placeholder="Type a response… Enter to send"
                    value={rs.draft}
                    disabled={rs.submitting}
                    onChange={function(e) { patchRespond(item.tool_call_id, { draft: e.target.value }); }}
                    onKeyDown={function(e) { handleRespondKeyDown(item, e); }}
                    style={{
                      flex: 1,
                      background: "var(--bg-2)",
                      border: "1px solid var(--border)",
                      borderRadius: 6,
                      padding: "4px 8px",
                      fontSize: 12,
                      color: "var(--text)",
                      fontFamily: "inherit",
                      outline: "none",
                    }}
                  />
                </div>
              )}

              {/* Cancel for watch_files / sleep */}
              {isCancelable && (
                <div data-testid="action-cancel-controls">
                  <button
                    type="button"
                    data-testid="cancel-yield"
                    onClick={function() { handleCancel(item); }}
                    style={{
                      padding: "4px 10px",
                      borderRadius: 6,
                      border: "1px solid var(--border)",
                      background: "transparent",
                      color: "var(--text-3)",
                      cursor: "pointer",
                      fontSize: 12,
                    }}
                  >
                    Cancel
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// WorkspaceActivity — thin header wrapper around the reused WorkspaceTap
// ---------------------------------------------------------------------------

function WorkspaceActivity({ wid }) {
  return (
    <div
      data-testid="workspace-activity"
      style={{
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
        overflow: "hidden",
      }}
    >
      {/* Section header */}
      <div
        style={{
          height: 34,
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "0 12px",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <span
          className="dot"
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: "var(--green)",
            display: "inline-block",
            flexShrink: 0,
          }}
        />
        <span
          style={{
            fontSize: 10.5,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            fontWeight: 600,
            color: "var(--text-2)",
            flex: 1,
          }}
        >
          Workspace Activity
        </span>
        <span
          style={{
            fontSize: 10,
            color: "var(--green)",
            fontFamily: "IBM Plex Mono, monospace",
          }}
        >
          live
        </span>
      </div>

      {/* WorkspaceTap owns its filter chips + SSE connection + auto-scroll */}
      <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <window.WorkspaceTap wid={wid} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// StudioActivity — right sidebar shell (B4)
// Replaces <ST_RegionPlaceholder testid="region-activity" /> in studio.jsx
// ---------------------------------------------------------------------------

function StudioActivity({ wid, studio }) {
  return (
    <div
      data-testid="studio-activity-root"
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        overflow: "hidden",
        borderLeft: "1px solid var(--border)",
      }}
    >
      <ActionRequired wid={wid} studio={studio} />
      <WorkspaceActivity wid={wid} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// No-build window exports
// ---------------------------------------------------------------------------
window.StudioActivity = StudioActivity;
window.ActionRequired = ActionRequired;
window.WorkspaceActivity = WorkspaceActivity;
