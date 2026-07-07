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

// Global (this file) -> inline (studio-center.jsx's ST_InlineYields) sync.
// The four raw-apiFetch handlers below aren't wired through useMutation's
// `invalidates` option, so a successful respond here only ever refreshed
// THIS component's own "studio-yields-pending:{wid}" resource (via hide()'s
// delayed pending.refetch()) — the session-scoped "session-adapter:pending:
// {sid}" cache SA_useSessionConversation polls for the inline panel was left
// to catch up on its own 4s poll. Force it immediately, using the exact same
// findKeys/refetchKey primitive ST_yieldInvalidates' callers (useMutation)
// use, and the exact same key string SA_useSessionConversation registers
// (session-adapter.jsx's `"session-adapter:pending:" + sid` resource key).
function SA_invalidateSessionPending(sessionId) {
  var resourceApi = window.primerApi && window.primerApi._resource;
  if (!resourceApi || !sessionId) return;
  var baseKey = "session-adapter:pending:" + sessionId;
  resourceApi.findKeys(baseKey).forEach(function(key) { resourceApi.refetchKey(key); });
}

// inform_user surfacing (investigated for PR "studio-activity-rework"):
//   inform_user is a NON-yielding tool — it relays a one-way message via
//   ctx.inform (SessionInformSink → channels) and returns immediately. It does
//   NOT park the session, so it NEVER appears in /workspaces/{wid}/yields/
//   pending. Its only observable trace is a `tool_call` frame in the workspace
//   tap: payload = {id, arguments:{message, files?}}. The persisted tool_call
//   record does not always carry the tool NAME (only ToolCallEnd's id+arguments
//   are recorded; the named ToolCallStart is dropped — see
//   primer/session/persistence.py), so we detect an inform by tool name when
//   present, else by the exact inform_user argument signature. Because inform
//   is fire-and-forget there is NO backend ack endpoint, so "Dismiss" is a
//   client-side removal only.
//
// Returns { key, session_id, message, ts } for an inform tool_call, else null.
function SA_informFromEvent(ev) {
  if (!ev || typeof ev !== "object" || ev.class !== "tool_call") return null;
  var payload = ev.payload && typeof ev.payload === "object" ? ev.payload : {};
  var args = payload.arguments != null ? payload.arguments : payload.args;
  if (typeof args === "string") {
    try { args = JSON.parse(args); } catch (_e) { args = null; }
  }
  if (!args || typeof args !== "object") return null;
  var message = typeof args.message === "string" ? args.message : "";
  if (!message.trim()) return null;

  var name = payload.tool_name || payload.name || args.name || "";
  var isInform = false;
  if (name) {
    // A named tool_call: accept only inform_user (e.g. "misc__inform_user").
    if (String(name).indexOf("inform_user") < 0) return null;
    isInform = true;
  } else {
    // No recorded name: accept ONLY the exact inform_user arg signature
    // ({message} plus at most a `files` companion) so a generic message-
    // bearing tool_call is never misclassified as an inform.
    var extra = Object.keys(args).filter(function (k) {
      return k !== "message" && k !== "files";
    });
    isInform = extra.length === 0;
  }
  if (!isInform) return null;

  var idPart = ev.seq != null ? ev.seq : (payload.id || "");
  return {
    key: (ev.session_id || "") + ":" + idPart,
    session_id: ev.session_id || "",
    message: message,
    ts: ev.ts || null,
  };
}

// ---------------------------------------------------------------------------
// ActionRequired
// ---------------------------------------------------------------------------

function ActionRequired({ wid, studio, onCountChange }) {
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

  // Live reconcile via the SHARED workspace tap (foundation/use-workspace-tap.js).
  // ONE EventSource for the whole Studio view feeds this reconcile listener,
  // the WorkspaceTap activity feed, and the graph run-view — instead of each
  // opening its own connection (fe-review N4). We debounce a refetch of the
  // pending snapshot on yielded/done; the 15s pollMs backstops a dropped tap.
  // inform_user items captured LIVE from the tap (see SA_informFromEvent).
  // They have no pending-yield backing, so they live in local state and are
  // dismissed client-side. Bounded so a chatty agent can't grow it unbounded.
  var [informItems, setInformItems] = React.useState([]);
  var [informDismissed, setInformDismissed] = React.useState({});

  var debounceRef = React.useRef(null);
  window.useWorkspaceTapListener(wid, function (ev) {
    if (!ev || typeof ev !== "object") return;
    var cls = ev.class;
    if (cls === "yielded" || cls === "done") {
      clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(function () { pending.refetch(); }, 300);
    }
    var inform = SA_informFromEvent(ev);
    if (inform) {
      setInformItems(function (prev) {
        for (var i = 0; i < prev.length; i++) {
          if (prev[i].key === inform.key) return prev;
        }
        var next = prev.concat(inform);
        if (next.length > 30) next = next.slice(next.length - 30);
        return next;
      });
    }
  });
  React.useEffect(function () {
    return function () { clearTimeout(debounceRef.current); };
  }, []);

  function dismissInform(key) {
    setInformDismissed(function (prev) {
      var next = {};
      for (var k in prev) next[k] = prev[k];
      next[key] = true;
      return next;
    });
  }

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

  // Action handlers. Each guards on a falsy tool_call_id — the /yields/pending
  // item shape allows tool_call_id: null (malformed/legacy parks), and every
  // endpoint below embeds it in the URL/body. Bailing keeps us from POSTing
  // e.g. /yields/null/cancel.
  function handleApprove(item) {
    if (!item.tool_call_id) return;
    apiFetch(
      "POST",
      "/sessions/" + encodeURIComponent(item.session_id) + "/tool_approval/respond",
      { tool_call_id: item.tool_call_id, decision: "approved" }
    ).then(function() {
      hide(item.tool_call_id);
      SA_invalidateSessionPending(item.session_id);
    }).catch(function(err) {
      // Surface error inline — stay visible so user can retry
      patchRespond(item.tool_call_id, { error: (err && (err.detail || err.title || err.message)) || "Approve failed" });
    });
  }

  function handleReject(item) {
    if (!item.tool_call_id) return;
    apiFetch(
      "POST",
      "/sessions/" + encodeURIComponent(item.session_id) + "/tool_approval/respond",
      { tool_call_id: item.tool_call_id, decision: "rejected", reason: "" }
    ).then(function() {
      hide(item.tool_call_id);
      SA_invalidateSessionPending(item.session_id);
    }).catch(function(err) {
      patchRespond(item.tool_call_id, { error: (err && (err.detail || err.title || err.message)) || "Reject failed" });
    });
  }

  function handleRespondSubmit(item) {
    if (!item.tool_call_id) return;
    var rs = getRespond(item.tool_call_id);
    if (!rs.draft.trim()) return;
    patchRespond(item.tool_call_id, { submitting: true, error: null });
    apiFetch(
      "POST",
      "/sessions/" + encodeURIComponent(item.session_id) + "/ask_user/respond",
      { tool_call_id: item.tool_call_id, response: rs.draft.trim() }
    ).then(function() {
      hide(item.tool_call_id);
      SA_invalidateSessionPending(item.session_id);
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
    if (!item.tool_call_id) return;
    apiFetch(
      "POST",
      "/sessions/" + encodeURIComponent(item.session_id) + "/yields/" + encodeURIComponent(item.tool_call_id) + "/cancel",
      { reason: "operator cancelled" }
    ).then(function() {
      hide(item.tool_call_id);
      SA_invalidateSessionPending(item.session_id);
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
  var visibleInform = informItems.filter(function (it) { return !informDismissed[it.key]; });
  var count = visibleItems.length + visibleInform.length;

  // Task 14: report the live count up to StudioActivity so the collapsed
  // rail can show a badge without a second fetch — ActionRequired stays the
  // single source of truth for "how many pending yields right now".
  React.useEffect(function() {
    if (typeof onCountChange === "function") onCountChange(count);
  }, [count, onCountChange]);

  return (
    <div
      className={"st-section st-action-required " + (count > 0 ? "has-items" : "is-empty")}
      data-testid="action-required"
    >
      {/* Section header */}
      <div
        className="st-panel-bar"
        style={{ borderBottom: count > 0 ? "1px solid var(--border)" : "none" }}
      >
        <span style={{ color: "var(--amber)", fontSize: 13 }} aria-hidden="true">⚠</span>
        <span className="st-section-label" data-testid="user-interaction-label">
          User Interaction
        </span>
        {count > 0 && (
          <span
            data-testid="action-required-count"
            className="st-pill"
            style={{
              background: "var(--amber-dim)",
              color: "var(--amber)",
              padding: "1px 7px",
              fontSize: 10.5,
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
          <div className="st-action-empty" data-testid="action-required-empty">
            No pending interactions.
          </div>
        )}

        {visibleItems.map(function(item, idx) {
          var rs = getRespond(item.tool_call_id);
          var isApproval = item.kind === "approval";
          var isAsk = item.kind === "ask_user";
          var isCancelable = item.kind === "watch_files" || item.kind === "sleep";
          var sidShort = SA_short(item.session_id);
          // A park with no tool_call_id (the /yields/pending shape allows null)
          // is still rendered, but its action controls are disabled so a
          // malformed/legacy item can't POST e.g. /yields/null/cancel.
          var actionable = !!item.tool_call_id;

          return (
            <div
              key={(item.session_id || "") + ":" + (item.tool_call_id || idx)}
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
              <div className="st-row" style={{ gap: 6 }}>
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
                    disabled={!actionable}
                    onClick={function() { handleApprove(item); }}
                    style={{
                      flex: 1,
                      padding: "4px 0",
                      borderRadius: 6,
                      border: "1px solid oklch(0.82 0.18 145 / 0.4)",
                      background: "var(--green-dim)",
                      color: "var(--green)",
                      cursor: actionable ? "pointer" : "not-allowed",
                      opacity: actionable ? 1 : 0.5,
                      fontSize: 12,
                      fontWeight: 600,
                    }}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    data-testid="reject"
                    disabled={!actionable}
                    onClick={function() { handleReject(item); }}
                    style={{
                      flex: 1,
                      padding: "4px 0",
                      borderRadius: 6,
                      border: "1px solid oklch(0.75 0.18 25 / 0.4)",
                      background: "var(--red-dim)",
                      color: "var(--red)",
                      cursor: actionable ? "pointer" : "not-allowed",
                      opacity: actionable ? 1 : 0.5,
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
                    placeholder="Type a response…"
                    value={rs.draft}
                    disabled={rs.submitting || !actionable}
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
                  {/* Visible Send button — the input is otherwise Enter-only
                      and thus undiscoverable. Same submit path as Enter. */}
                  <button
                    type="button"
                    data-testid="ask-user-send"
                    title="Send response"
                    disabled={rs.submitting || !actionable || !rs.draft.trim()}
                    onClick={function() { handleRespondSubmit(item); }}
                    style={{
                      flexShrink: 0,
                      padding: "4px 12px",
                      borderRadius: 6,
                      border: "1px solid var(--border-strong)",
                      background: "var(--bg-active)",
                      color: "var(--text)",
                      cursor: (rs.submitting || !actionable || !rs.draft.trim()) ? "not-allowed" : "pointer",
                      opacity: (rs.submitting || !actionable || !rs.draft.trim()) ? 0.5 : 1,
                      fontSize: 12,
                      fontWeight: 600,
                    }}
                  >
                    Send
                  </button>
                </div>
              )}

              {/* Cancel for watch_files / sleep */}
              {isCancelable && (
                <div data-testid="action-cancel-controls">
                  <button
                    type="button"
                    data-testid="cancel-yield"
                    disabled={!actionable}
                    onClick={function() { handleCancel(item); }}
                    style={{
                      padding: "4px 10px",
                      borderRadius: 6,
                      border: "1px solid var(--border)",
                      background: "transparent",
                      color: "var(--text-3)",
                      cursor: actionable ? "pointer" : "not-allowed",
                      opacity: actionable ? 1 : 0.5,
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

        {/* inform_user items — one-way messages surfaced live from the tap.
            No pending yield backs them, so Dismiss is a client-side removal. */}
        {visibleInform.map(function (inf) {
          return (
            <div
              key={"inform:" + inf.key}
              data-testid="inform-item"
              style={{
                padding: "10px 12px",
                borderBottom: "1px solid var(--border)",
                display: "flex",
                flexDirection: "column",
                gap: 6,
              }}
            >
              {/* Session label + kind */}
              <div className="st-row" style={{ gap: 6 }}>
                <button
                  type="button"
                  data-testid="action-session-link"
                  onClick={function() { handleFocusSession(inf.session_id); }}
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
                  title={inf.session_id}
                >
                  {SA_short(inf.session_id)}
                </button>
                <span
                  style={{
                    color: "var(--blue)",
                    fontSize: 10,
                    textTransform: "uppercase",
                    letterSpacing: "0.05em",
                    fontWeight: 600,
                  }}
                >
                  inform_user
                </span>
              </div>

              {/* Message body — grows to fit, scrolls past a sensible cap. */}
              <div
                data-testid="inform-message"
                style={{
                  fontSize: 12,
                  lineHeight: 1.5,
                  color: "var(--text-2)",
                  wordBreak: "break-word",
                  whiteSpace: "pre-wrap",
                  maxHeight: 140,
                  overflowY: "auto",
                }}
              >
                {inf.message}
              </div>

              {/* Ack — inform has no backend ack endpoint (fire-and-forget),
                  so this only clears it from the panel. */}
              <div>
                <button
                  type="button"
                  data-testid="inform-dismiss"
                  onClick={function() { dismissInform(inf.key); }}
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
                  Dismiss
                </button>
              </div>
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
        className="st-panel-bar"
        style={{ borderBottom: "1px solid var(--border)" }}
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
        <span className="st-section-label">
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
// StudioActivity — right sidebar shell (B4 -> Task 14 collapsed debug view)
// Replaces <ST_RegionPlaceholder testid="region-activity" /> in studio.jsx
//
// Task 14: this column is the GLOBAL debug tracker (Action Required across
// ALL sessions + the workspace-wide WorkspaceTap feed) — but it starts
// COLLAPSED so an operator who isn't actively debugging doesn't lose the
// screen real estate. Collapsing is purely a visual toggle: ActionRequired
// and WorkspaceActivity stay MOUNTED at all times (only the wrapping
// `debug-sidebar-body` div's display flips), so their poll timers + tap
// EventSources never tear down/reconnect on every expand — the badge count
// on the collapsed rail is always live, and re-expanding shows already-warm
// data instead of a cold fetch.
// ---------------------------------------------------------------------------

function StudioActivity({ wid, studio }) {
  // Collapsed state is owned by the studio store (studio.state.debugOpen) so the
  // header Debug toggle and this rail's own handle share ONE source of truth —
  // the internal-only useState meant the header could never reach it. Default is
  // collapsed (debugOpen:false in ST_defaultState).
  var collapsed = !(studio && studio.state && studio.state.debugOpen);
  var [pendingCount, setPendingCount] = React.useState(0);
  // Task (studio-ux fix 1): only the bell+"Debug" label retract into the
  // thin rail — the chevron (the toggle affordance itself) and the pending
  // badge stay visible in both states. A separate, positively-named flag
  // (rather than negating `collapsed` inline at each call site) keeps this
  // JSX visibly distinct from the ActionRequired/WorkspaceActivity
  // conditional-mount anti-pattern the sibling mount-guard test forbids
  // (see test_action_required_and_workspace_activity_stay_mounted_when_collapsed).
  var expanded = !collapsed;

  function toggle() {
    if (studio && typeof studio.toggleDebug === "function") studio.toggleDebug();
  }

  return (
    <div
      data-testid="studio-activity-root"
      // The collapse CSS keys off the `studio-activity-root` CLASS
      // (`.st-body:has(.studio-activity-root.is-collapsed)` shrinks the grid
      // track; `.studio-activity-root.is-collapsed { width: 40px }` shrinks the
      // rail). `studio-activity-root` was previously ONLY a data-testid, so
      // neither selector ever matched and the rail never actually collapsed —
      // it MUST be a real className for the rules to engage.
      className={"studio-activity-root" + (collapsed ? " is-collapsed" : "")}
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        overflow: "hidden",
        borderLeft: "1px solid var(--border)",
      }}
    >
      <button
        type="button"
        data-testid="debug-sidebar-toggle"
        aria-expanded={collapsed ? "false" : "true"}
        aria-controls="debug-sidebar-body"
        aria-label={collapsed ? "Expand debug panel" : "Collapse debug panel"}
        title={collapsed ? "Expand debug panel (Action Required + Activity)" : "Collapse debug panel"}
        onClick={toggle}
        style={{
          flexShrink: 0,
          display: "flex",
          flexDirection: collapsed ? "column" : "row",
          alignItems: "center",
          justifyContent: "center",
          gap: collapsed ? 6 : 8,
          height: collapsed ? "auto" : 34,
          minHeight: collapsed ? 64 : "auto",
          padding: collapsed ? "10px 4px" : "0 12px",
          border: "none",
          borderBottom: collapsed ? "none" : "1px solid var(--border)",
          background: "transparent",
          color: "var(--text-2)",
          cursor: "pointer",
          fontSize: 10.5,
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          fontWeight: 600,
          font: "inherit",
        }}
      >
        <Icon name={collapsed ? "chevron-left" : "chevron-right"} size={13} style={{ flexShrink: 0, color: "var(--text-3)" }} />
        {expanded && <Icon name="bell" size={13} style={{ flexShrink: 0 }} />}
        {expanded && <span style={{ flex: 1, textAlign: "left" }}>Debug</span>}
        {/* Collapsed: a vertical "Debug" label so the thin rail is legibly the
            expand handle. The chevron alone was too subtle — operators couldn't
            tell the strip was clickable (or that it was the debug panel). */}
        {collapsed && (
          <span
            data-testid="debug-sidebar-rail-label"
            style={{ writingMode: "vertical-rl", letterSpacing: "0.12em", color: "var(--text-3)" }}
          >
            Debug
          </span>
        )}
        {pendingCount > 0 && (
          <span
            data-testid="debug-sidebar-badge"
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
            {pendingCount}
          </span>
        )}
      </button>

      <div
        id="debug-sidebar-body"
        data-testid="debug-sidebar-body"
        style={{
          display: collapsed ? "none" : "flex",
          flexDirection: "column",
          flex: 1,
          minHeight: 0,
          overflow: "hidden",
        }}
      >
        <ActionRequired wid={wid} studio={studio} onCountChange={setPendingCount} />
        <WorkspaceActivity wid={wid} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// No-build window exports
// ---------------------------------------------------------------------------
window.StudioActivity = StudioActivity;
window.ActionRequired = ActionRequired;
window.WorkspaceActivity = WorkspaceActivity;
