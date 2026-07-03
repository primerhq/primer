/* global React, Icon, Modal, Btn, Banner */
// StudioSidebar — left sidebar for the Studio IDE shell (PR-B / B2).
//
// Contains two collapsible sections:
//   SessionsSection  — live session list with status dots; "+" new-session form.
//   FilesTree        — lazy-expanding file tree; show-hidden toggle; refresh.
//
// Plugs into the Studio shell's left region (replaces the ST_RegionPlaceholder
// at data-testid="region-sidebar" inside data-testid="studio-sidebar").
// Receives the studio state-bag from Studio({ wid }) via the `studio` prop.
//
// No-build rules: top-level declarations use `var`; helpers prefixed ST_;
// every exported symbol is assigned to window.X at the bottom.

// ---------------------------------------------------------------------------
// sessionStatus — shared status-derivation helper
// Derives { tone, label, badge } from a session row returned by
//   GET /v1/workspaces/{wid}/sessions
// Fields inspected:
//   session.status          : "created"|"running"|"paused"|"ended"|"failed"|...
//   session.parked_status   : "parked"|"resumable"|undefined
//   session.parked_state    : object with { yielded?: { tool_name }, tool_name? }
//
// Published as window.ST_sessionStatus for B3/B4 reuse.
// ---------------------------------------------------------------------------

function ST_sessionStatus(session) {
  if (!session) return { tone: "dim", label: "unknown", badge: null };

  var status = session.status || "created";

  // Resolve the parked tool when session is parked/waiting.
  var isParked = session.parked_status === "parked" || session.parked_status === "resumable";
  var parkedTool = null;
  if (isParked && session.parked_state) {
    var ps = session.parked_state;
    parkedTool = (ps.yielded && ps.yielded.tool_name) || ps.tool_name || null;
  }

  if (status === "running" && !isParked) {
    return { tone: "green-pulse", label: "running", badge: null };
  }
  if (status === "paused") {
    return { tone: "amber", label: "paused", badge: null };
  }
  if (isParked) {
    if (parkedTool === "approval" || parkedTool === "ask_approval") {
      return { tone: "amber", label: "waiting", badge: "approve" };
    }
    if (parkedTool === "ask_user") {
      return { tone: "amber", label: "waiting", badge: "ask" };
    }
    if (parkedTool === "watch_files") {
      return { tone: "amber", label: "waiting", badge: "watch" };
    }
    if (parkedTool === "sleep") {
      return { tone: "amber", label: "waiting", badge: "sleep" };
    }
    // Generic waiting state.
    return { tone: "amber", label: "waiting", badge: null };
  }
  if (status === "created") {
    return { tone: "dim", label: "created", badge: null };
  }
  if (status === "ended" || status === "completed" || status === "cancelled") {
    return { tone: "gray", label: status, badge: null };
  }
  if (status === "failed") {
    return { tone: "red", label: "failed", badge: null };
  }
  // Fallback.
  return { tone: "dim", label: status, badge: null };
}

// ---------------------------------------------------------------------------
// ST_sessionKind — classify a session row as "graph" or "agent".
//
// The workspace sessions list endpoint returns SessionInfo, which has NO
// `binding` field — graph-bound sessions instead carry a SYNTHETIC
// `agent_id = "graph:<graph_id>"` (see primer/workspace/session_factory.py:
// the graph holder slot). So the prefix is the signal for the list shape.
// We also honour the fuller WorkspaceSession / create-response shape
// (`binding.kind` / `binding_kind` / bare `graph_id`) so the same helper
// works wherever a session object comes from.
//
// Published as window.ST_sessionKind / window.ST_sessionGlyph for B3 reuse.
// ---------------------------------------------------------------------------

function ST_sessionKind(session) {
  if (!session) return "agent";
  var aid = session.agent_id || "";
  if (typeof aid === "string" && aid.indexOf("graph:") === 0) return "graph";
  if (session.binding && session.binding.kind === "graph") return "graph";
  if (session.binding_kind === "graph") return "graph";
  if (session.graph_id && !session.agent_id) return "graph";
  return "agent";
}

function ST_sessionGlyph(session) {
  return ST_sessionKind(session) === "graph" ? "◈" : "◆";
}

// ---------------------------------------------------------------------------
// ST_dotStyle — build an inline style object for the 8×8 status dot.
// ---------------------------------------------------------------------------

function ST_dotStyle(tone) {
  var base = {
    width: 8,
    height: 8,
    borderRadius: "50%",
    flexShrink: 0,
    display: "inline-block",
  };
  if (tone === "green-pulse") {
    return Object.assign({}, base, {
      background: "var(--green)",
      animation: "pulse 1.8s ease-in-out infinite",
    });
  }
  if (tone === "amber") {
    return Object.assign({}, base, {
      background: "var(--amber)",
      boxShadow: "inset 0 0 0 2px var(--amber-dim)",
    });
  }
  if (tone === "red") {
    return Object.assign({}, base, { background: "var(--red)" });
  }
  if (tone === "gray") {
    return Object.assign({}, base, { background: "var(--text-4)" });
  }
  // dim (created / unknown)
  return Object.assign({}, base, { background: "var(--border-strong)" });
}

// ---------------------------------------------------------------------------
// ST_sessionSort — active/waiting-first, then by id for stability.
// ---------------------------------------------------------------------------

function ST_sessionSort(sessions) {
  var order = { "running": 0, "paused": 1, "created": 2 };
  return sessions.slice().sort(function(a, b) {
    var oa = order[a.status] !== undefined ? order[a.status] : 3;
    var ob = order[b.status] !== undefined ? order[b.status] : 3;
    if (oa !== ob) return oa - ob;
    // Parked (waiting) sessions float up within their group.
    var aParked = a.parked_status === "parked" || a.parked_status === "resumable" ? -1 : 0;
    var bParked = b.parked_status === "parked" || b.parked_status === "resumable" ? -1 : 0;
    if (aParked !== bParked) return aParked - bParked;
    // The list endpoint returns SessionInfo, which carries `session_id`
    // (not `id`). Fall back to `id` for the create-response / detail shapes.
    var aId = a.session_id || a.id || "";
    var bId = b.session_id || b.id || "";
    return aId < bId ? -1 : aId > bId ? 1 : 0;
  });
}

// ---------------------------------------------------------------------------
// ST_fileIconName — map a file extension / dir to an Icon name.
// ---------------------------------------------------------------------------

function ST_fileIconName(item) {
  if (item.is_dir) return "box";
  var ext = (item.name || "").split(".").pop().toLowerCase();
  if (ext === "py" || ext === "js" || ext === "jsx" || ext === "ts" || ext === "tsx") return "code";
  if (ext === "md" || ext === "txt" || ext === "rst") return "doc";
  if (ext === "json" || ext === "yaml" || ext === "yml" || ext === "toml") return "code";
  if (ext === "png" || ext === "jpg" || ext === "jpeg" || ext === "svg" || ext === "gif" || ext === "webp") return "image";
  if (ext === "sh" || ext === "bash" || ext === "zsh") return "code";
  return "file";
}

// ---------------------------------------------------------------------------
// ST_fileIconColor — colour the icon based on extension.
// ---------------------------------------------------------------------------

function ST_fileIconColor(item) {
  if (item.is_dir) return "var(--text-3)";
  var ext = (item.name || "").split(".").pop().toLowerCase();
  if (ext === "py") return "var(--blue)";
  if (ext === "md" || ext === "txt" || ext === "rst") return "var(--text-2)";
  return "var(--text-3)";
}

// ---------------------------------------------------------------------------
// NewSessionForm — inline modal-style form for creating a session.
// Renders as a positioned overlay inside the sessions section.
// POST /v1/workspaces/{wid}/sessions with the SessionCreateBody shape:
//   { binding: { kind, agent_id? | graph_id? }, auto_start, initial_instructions? }
// ---------------------------------------------------------------------------

function NewSessionForm({ wid, onClose, onCreated }) {
  var { useResource, apiFetch } = window.primerApi;

  var agents = useResource(
    "new-session-form:agents",
    function(signal) { return apiFetch("GET", "/agents?limit=200", null, { signal }); },
    { pollMs: 0 }
  );
  var graphs = useResource(
    "new-session-form:graphs",
    function(signal) { return apiFetch("GET", "/graphs?limit=200", null, { signal }); },
    { pollMs: 0 }
  );

  var agentItems = (agents.data && agents.data.items) ? agents.data.items : [];
  var graphItems = (graphs.data && graphs.data.items) ? graphs.data.items : [];

  var [kind, setKind] = React.useState("agent");
  var [agentId, setAgentId] = React.useState("");
  var [graphId, setGraphId] = React.useState("");
  var [name, setName] = React.useState("");
  var [instructions, setInstructions] = React.useState("");
  var [submitting, setSubmitting] = React.useState(false);
  var [error, setError] = React.useState(null);

  // Single owner of the default selection: these effects default the first
  // available option whenever items load OR the kind toggles to an as-yet
  // unselected binding. The AGENT/GRAPH toggle buttons below only change
  // `kind` — they do NOT set the id — so there is exactly one writer per id
  // and no race between the effect and an imperative button handler.
  React.useEffect(function() {
    if (kind === "agent" && agentItems.length > 0 && !agentId) {
      setAgentId(agentItems[0].id);
    }
  }, [agentItems, kind]);
  React.useEffect(function() {
    if (kind === "graph" && graphItems.length > 0 && !graphId) {
      setGraphId(graphItems[0].id);
    }
  }, [graphItems, kind]);

  var loading = agents.loading || graphs.loading;
  var canSubmit = !submitting && (kind === "agent" ? agentId : graphId);

  async function onSubmit(e) {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    var binding = kind === "agent"
      ? { kind: "agent", agent_id: agentId }
      : { kind: "graph", graph_id: graphId };
    var body = { binding: binding, auto_start: true };
    if (name.trim()) body.name = name.trim();
    if (instructions.trim()) body.initial_instructions = instructions.trim();
    try {
      var session = await apiFetch("POST", "/workspaces/" + encodeURIComponent(wid) + "/sessions", body);
      onCreated(session);
    } catch (err) {
      setError((err && err.detail) || (err && err.message) || "Failed to create session");
      setSubmitting(false);
    }
  }

  return (
    <div
      style={{
        position: "absolute",
        top: 32,
        left: 0,
        right: 0,
        zIndex: 20,
        background: "var(--bg-elev)",
        border: "1px solid var(--border-strong)",
        borderRadius: 9,
        boxShadow: "var(--shadow)",
        padding: "12px 12px 10px",
        margin: "4px 6px",
      }}
      data-testid="new-session-form"
    >
      <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-3)", flex: 1 }}>New session</span>
        <button
          style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-3)", fontSize: 14, padding: "0 2px", lineHeight: 1 }}
          onClick={onClose}
          title="Cancel"
        >×</button>
      </div>

      {/* Optional friendly name — persisted onto the session so the sidebar
          shows it instead of the opaque sess-<hex> id. */}
      <input
        data-testid="new-session-name"
        placeholder="Name (optional)"
        value={name}
        onChange={function(e) { setName(e.target.value); }}
        style={{
          width: "100%", padding: "5px 7px", fontSize: 12, background: "var(--bg-2)",
          border: "1px solid var(--border)", borderRadius: 5, color: "var(--text)",
          marginBottom: 8, outline: "none", fontFamily: "inherit",
        }}
      />

      {/* Binding kind toggle */}
      <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
        <button
          style={{
            flex: 1,
            padding: "4px 0",
            fontSize: 11,
            fontWeight: 600,
            border: "1px solid " + (kind === "agent" ? "var(--accent-border)" : "var(--border)"),
            borderRadius: 5,
            background: kind === "agent" ? "var(--accent-dim)" : "var(--bg-2)",
            color: kind === "agent" ? "var(--accent)" : "var(--text-3)",
            cursor: "pointer",
          }}
          onClick={function() { setKind("agent"); }}
        >AGENT</button>
        <button
          style={{
            flex: 1,
            padding: "4px 0",
            fontSize: 11,
            fontWeight: 600,
            border: "1px solid " + (kind === "graph" ? "var(--accent-border)" : "var(--border)"),
            borderRadius: 5,
            background: kind === "graph" ? "var(--accent-dim)" : "var(--bg-2)",
            color: kind === "graph" ? "var(--accent)" : "var(--text-3)",
            cursor: "pointer",
          }}
          onClick={function() { setKind("graph"); }}
        >GRAPH</button>
      </div>

      {/* Binding selector */}
      {kind === "agent" ? (
        <select
          style={{ width: "100%", padding: "5px 7px", fontSize: 12, background: "var(--bg-2)", border: "1px solid var(--border)", borderRadius: 5, color: "var(--text)", marginBottom: 8 }}
          value={agentId}
          onChange={function(e) { setAgentId(e.target.value); }}
          disabled={loading || agentItems.length === 0}
        >
          {agentItems.length === 0 && <option value="">{loading ? "Loading…" : "No agents"}</option>}
          {agentItems.map(function(a) { return <option key={a.id} value={a.id}>{a.id}</option>; })}
        </select>
      ) : (
        <select
          style={{ width: "100%", padding: "5px 7px", fontSize: 12, background: "var(--bg-2)", border: "1px solid var(--border)", borderRadius: 5, color: "var(--text)", marginBottom: 8 }}
          value={graphId}
          onChange={function(e) { setGraphId(e.target.value); }}
          disabled={loading || graphItems.length === 0}
        >
          {graphItems.length === 0 && <option value="">{loading ? "Loading…" : "No graphs"}</option>}
          {graphItems.map(function(g) { return <option key={g.id} value={g.id}>{g.id}</option>; })}
        </select>
      )}

      {/* Instructions */}
      <textarea
        placeholder="Initial instructions (optional)"
        value={instructions}
        onChange={function(e) { setInstructions(e.target.value); }}
        rows={3}
        style={{
          width: "100%",
          padding: "5px 7px",
          fontSize: 12,
          background: "var(--bg-2)",
          border: "1px solid var(--border)",
          borderRadius: 5,
          color: "var(--text)",
          resize: "none",
          fontFamily: "inherit",
          marginBottom: 8,
          outline: "none",
        }}
      />

      {error && (
        <div style={{ fontSize: 11, color: "var(--red)", marginBottom: 6 }}>{error}</div>
      )}

      <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
        <button
          style={{ padding: "4px 10px", fontSize: 12, border: "1px solid var(--border)", borderRadius: 5, background: "var(--bg-2)", color: "var(--text-2)", cursor: "pointer" }}
          onClick={onClose}
        >Cancel</button>
        <button
          style={{
            padding: "4px 10px",
            fontSize: 12,
            fontWeight: 600,
            border: "1px solid var(--accent-border)",
            borderRadius: 5,
            background: canSubmit ? "var(--accent-dim)" : "var(--bg-2)",
            color: canSubmit ? "var(--accent)" : "var(--text-4)",
            cursor: canSubmit ? "pointer" : "default",
          }}
          disabled={!canSubmit}
          onClick={onSubmit}
        >{submitting ? "Creating…" : "Create"}</button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ST_SessionDeleteDialog — confirm + DELETE a session.
//   DELETE /v1/workspaces/{wid}/sessions/{sid}. Uses the shared Modal (NOT
//   native confirm()). On success the caller closes the matching center tab
//   and refetches the sidebar list; failures surface inline + as a toast.
// ---------------------------------------------------------------------------

function ST_SessionDeleteDialog({ wid, session, onClose, onDeleted, pushToast }) {
  var { apiFetch } = window.primerApi;
  var sid = session.session_id || session.id;
  var label = session.name || sid;
  var [busy, setBusy] = React.useState(false);
  var [error, setError] = React.useState(null);

  var mountedRef = React.useRef(true);
  React.useEffect(function () {
    mountedRef.current = true;
    return function () { mountedRef.current = false; };
  }, []);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await apiFetch("DELETE", "/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid));
      if (!mountedRef.current) return;
      pushToast && pushToast({ kind: "success", title: "Session deleted", detail: label });
      onDeleted && onDeleted(sid);
    } catch (err) {
      if (!mountedRef.current) return;
      var detail = (err && (err.detail || err.message)) || "Delete failed";
      setError(detail);
      setBusy(false);
      pushToast && pushToast({
        kind: "error",
        title: "Delete failed",
        detail: detail,
        requestId: err && err.requestId,
      });
    }
  }

  return (
    <Modal
      title={"Delete session · " + label}
      danger
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="danger"
            icon="trash"
            onClick={submit}
            disabled={busy}
            data-testid="session-delete-confirm"
          >
            {busy ? "Deleting…" : "Delete session"}
          </Btn>
        </>
      }
    >
      <div data-testid="session-delete-confirm-body">
        <p>Permanently remove this session and its on-disk state. A running
        session is cancelled first. This cannot be undone.</p>
        {error && (
          <Banner kind="error" title="Delete failed" detail={String(error)} />
        )}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// ST_SessionRenameDialog — give a session a friendly name (or clear it).
//   PATCH /v1/workspaces/{wid}/sessions/{sid} { name }. An empty value
//   clears the name (server falls back to the id). Returns the updated
//   SessionInfo; the caller refetches so the row reflects the new label.
// ---------------------------------------------------------------------------

function ST_SessionRenameDialog({ wid, session, onClose, onRenamed, pushToast }) {
  var { apiFetch } = window.primerApi;
  var sid = session.session_id || session.id;
  var [name, setName] = React.useState(session.name || "");
  var [busy, setBusy] = React.useState(false);
  var [error, setError] = React.useState(null);

  var mountedRef = React.useRef(true);
  React.useEffect(function () {
    mountedRef.current = true;
    return function () { mountedRef.current = false; };
  }, []);

  async function submit(e) {
    if (e && e.preventDefault) e.preventDefault();
    setBusy(true);
    setError(null);
    var trimmed = name.trim();
    try {
      await apiFetch(
        "PATCH",
        "/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid),
        { name: trimmed ? trimmed : null }
      );
      if (!mountedRef.current) return;
      pushToast && pushToast({ kind: "success", title: "Session renamed" });
      onRenamed && onRenamed(sid);
    } catch (err) {
      if (!mountedRef.current) return;
      var detail = (err && (err.detail || err.message)) || "Rename failed";
      setError(detail);
      setBusy(false);
    }
  }

  return (
    <Modal
      title={"Rename session · " + sid}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="edit"
            onClick={submit}
            disabled={busy}
            data-testid="session-rename-confirm"
          >
            {busy ? "Saving…" : "Save name"}
          </Btn>
        </>
      }
    >
      <form onSubmit={submit} data-testid="session-rename-body">
        <input
          data-testid="session-rename-input"
          autoFocus
          value={name}
          onChange={function (e) { setName(e.target.value); }}
          placeholder="Friendly name (leave blank to clear)"
          style={{
            width: "100%", padding: "7px 9px", fontSize: 13, background: "var(--bg-2)",
            border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)",
            outline: "none", fontFamily: "inherit",
          }}
        />
        {error && (
          <div style={{ marginTop: 8 }}>
            <Banner kind="error" title="Rename failed" detail={String(error)} />
          </div>
        )}
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// SessionsSection
// ---------------------------------------------------------------------------

function SessionsSection({ wid, studio }) {
  var { useResource, apiFetch } = window.primerApi;
  var s = studio.state;

  var sessionsRes = useResource(
    "studio-sessions:" + wid,
    function(signal) { return apiFetch("GET", "/workspaces/" + encodeURIComponent(wid) + "/sessions", null, { signal }); },
    { pollMs: 3000 }
  );

  var rawSessions = (sessionsRes.data && Array.isArray(sessionsRes.data.items))
    ? sessionsRes.data.items
    : (Array.isArray(sessionsRes.data) ? sessionsRes.data : []);

  var sessions = ST_sessionSort(rawSessions);

  // New-session form visibility is owned by studio state (studio.jsx) so the
  // ⌘K palette's "New session" action can open the SAME form this section's
  // "+" button does. See FB6.
  var showNewForm = studio.newSessionOpen;
  // Session pending a delete confirm (the row whose trash button was hit),
  // and the session being renamed inline. Both null when idle.
  var [pendingDelete, setPendingDelete] = React.useState(null);
  var [renaming, setRenaming] = React.useState(null);
  var pushToast = studio.pushToast || (window.primerApi && window.primerApi.toastPush) || null;

  // A session was deleted: close its center tab (if open) and refetch the
  // sidebar list so the row disappears.
  function onSessionDeleted(sid) {
    setPendingDelete(null);
    studio.closeTab && studio.closeTab("session:" + sid);
    sessionsRes.refetch && sessionsRes.refetch();
  }

  // A session was renamed: clear the inline editor and refetch so the new
  // name (persisted onto session.json) shows in the row.
  function onSessionRenamed() {
    setRenaming(null);
    sessionsRes.refetch && sessionsRes.refetch();
  }

  function openSession(session) {
    // The list endpoint returns SessionInfo (`session_id`); the create
    // response / detail fetch use the fuller shape (`id`). Resolve either.
    var sid = session.session_id || session.id;
    var title = session.name || sid;
    var glyph = ST_sessionGlyph(session);
    studio.openTab({
      id: "session:" + sid,
      kind: "session",
      ref: sid,
      title: title,
      glyph: glyph,
    });
  }

  function onCreated(session) {
    studio.closeNewSession();
    // Invalidate the sessions resource so the new session appears.
    sessionsRes.refetch && sessionsRes.refetch();
    openSession(session);
  }

  var chevStyle = {
    display: "inline-block",
    transition: "transform 0.15s",
    transform: s.sessionsOpen ? "rotate(0deg)" : "rotate(-90deg)",
    color: "var(--text-4)",
  };

  return (
    <div
      className="st-section"
      style={{ flex: s.sessionsOpen ? "0 0 auto" : "0 0 auto", maxHeight: s.sessionsOpen ? "46%" : "32px", minHeight: 0, position: "relative" }}
      data-testid="sessions-section"
    >
      {/* Section header */}
      <div
        className="st-section-h"
        data-testid="sessions-header"
        onClick={studio.toggleSessions}
      >
        <span style={chevStyle}>▾</span>
        Sessions
        <span className="st-section-count">{sessions.length > 0 ? sessions.length : ""}</span>
        <button
          style={{
            width: 20,
            height: 20,
            display: "grid",
            placeItems: "center",
            borderRadius: 5,
            border: "none",
            background: "none",
            color: "var(--text-3)",
            fontSize: 14,
            cursor: "pointer",
            flexShrink: 0,
          }}
          title="New session"
          data-testid="new-session-btn"
          onClick={function(e) {
            e.stopPropagation();
            if (studio.newSessionOpen) studio.closeNewSession();
            else studio.openNewSession();
          }}
        >＋</button>
      </div>

      {/* New session form overlay */}
      {showNewForm && (
        <NewSessionForm
          wid={wid}
          onClose={function() { studio.closeNewSession(); }}
          onCreated={onCreated}
        />
      )}

      {/* Delete-session confirm (shared Modal, not native confirm()). */}
      {pendingDelete && (
        <ST_SessionDeleteDialog
          wid={wid}
          session={pendingDelete}
          pushToast={pushToast}
          onClose={function() { setPendingDelete(null); }}
          onDeleted={onSessionDeleted}
        />
      )}

      {/* Rename-session prompt. */}
      {renaming && (
        <ST_SessionRenameDialog
          wid={wid}
          session={renaming}
          pushToast={pushToast}
          onClose={function() { setRenaming(null); }}
          onRenamed={onSessionRenamed}
        />
      )}

      {/* Session list */}
      {s.sessionsOpen && (
        <div className="st-section-body">
          {sessionsRes.loading && sessions.length === 0 && (
            <div style={{ padding: "8px 12px", fontSize: 11, color: "var(--text-4)" }}>Loading…</div>
          )}
          {!sessionsRes.loading && sessions.length === 0 && (
            <div style={{ padding: "8px 12px", fontSize: 11, color: "var(--text-4)" }}>No sessions yet.</div>
          )}
          {sessions.map(function(session) {
            var st = ST_sessionStatus(session);
            // SessionInfo (list endpoint) carries `session_id`, not `id`;
            // the create/detail shapes carry `id`. Resolve either so the
            // row key, tab id, title, and data-session-id are the real id.
            var sid = session.session_id || session.id;
            var isGraph = ST_sessionKind(session) === "graph";
            var tabId = "session:" + sid;
            var isActive = s.activeTabId === tabId;
            var title = session.name || sid;

            return (
              <div
                key={sid}
                className="st-session-row"
                data-testid="session-row"
                data-session-id={sid}
                onClick={function() { openSession(session); }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 7,
                  height: "var(--row-h, 34px)",
                  padding: "0 10px 0 10px",
                  cursor: "pointer",
                  borderLeft: "2px solid " + (isActive ? "var(--accent)" : "transparent"),
                  background: isActive ? "var(--bg-active)" : "transparent",
                }}
              >
                <span
                  className="st-session-dot"
                  data-testid="session-status-dot"
                  style={ST_dotStyle(st.tone)}
                />
                <span style={{
                  flex: 1,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  color: (st.tone === "gray") ? "var(--text-3)" : "var(--text)",
                  fontSize: 12,
                }}>
                  {title}
                </span>
                {st.badge && (
                  <span style={{
                    fontSize: 9,
                    fontWeight: 700,
                    padding: "1px 5px",
                    borderRadius: 999,
                    background: "var(--amber-dim)",
                    color: "var(--amber)",
                    flexShrink: 0,
                  }}>
                    {st.badge}
                  </span>
                )}
                {/* Hover/focus-revealed row actions (rename · delete). The
                    stopPropagation keeps the row's open-on-click from firing
                    when an action button is pressed. */}
                <span className="st-row-actions">
                  <button
                    className="st-row-action"
                    data-testid="session-rename"
                    title="Rename session"
                    aria-label="Rename session"
                    onClick={function(e) { e.stopPropagation(); setRenaming(session); }}
                  >
                    <Icon name="edit" size={12} />
                  </button>
                  <button
                    className="st-row-action is-danger"
                    data-testid="session-delete"
                    title="Delete session"
                    aria-label="Delete session"
                    onClick={function(e) { e.stopPropagation(); setPendingDelete(session); }}
                  >
                    <Icon name="trash" size={12} />
                  </button>
                </span>
                <span style={{
                  fontSize: 9,
                  fontWeight: 700,
                  letterSpacing: "0.05em",
                  color: "var(--text-4)",
                  border: "1px solid var(--border)",
                  borderRadius: 4,
                  padding: "0 4px",
                  flexShrink: 0,
                }}>
                  {isGraph ? "GRAPH" : "AGENT"}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// FilesTree
// ---------------------------------------------------------------------------

function FilesTree({ wid, studio }) {
  var { useResource, apiFetch } = window.primerApi;
  var s = studio.state;

  // Root-level tree fetch. Refetches when showHidden changes.
  var rootRes = useResource(
    "studio-files-root:" + wid + ":" + s.showHidden,
    function(signal) {
      return apiFetch(
        "GET",
        "/workspaces/" + encodeURIComponent(wid) + "/files/tree?path=.&hidden=" + s.showHidden,
        null,
        { signal }
      );
    },
    { pollMs: 0 }
  );

  // Per-folder lazy-fetch cache keyed by path. Map: path → { loading, items, loaded }.
  var [folderCache, setFolderCache] = React.useState({});

  // When showHidden changes, clear the folder cache so child expansions
  // re-fetch with the updated hidden flag.
  var prevHiddenRef = React.useRef(s.showHidden);
  React.useEffect(function() {
    if (prevHiddenRef.current !== s.showHidden) {
      prevHiddenRef.current = s.showHidden;
      setFolderCache({});
    }
  }, [s.showHidden]);

  function fetchFolder(path) {
    // Only fetch once per path (or if not yet started).
    if (folderCache[path]) return;
    // Mark loading.
    setFolderCache(function(c) {
      var next = Object.assign({}, c);
      next[path] = { loading: true, items: [], loaded: false };
      return next;
    });
    apiFetch(
      "GET",
      "/workspaces/" + encodeURIComponent(wid) + "/files/tree?path=" + encodeURIComponent(path) + "&hidden=" + s.showHidden
    ).then(function(data) {
      var items = (data && data.items) ? data.items : [];
      setFolderCache(function(c) {
        var next = Object.assign({}, c);
        next[path] = { loading: false, items: items, loaded: true };
        return next;
      });
    }).catch(function() {
      setFolderCache(function(c) {
        var next = Object.assign({}, c);
        next[path] = { loading: false, items: [], loaded: true, error: true };
        return next;
      });
    });
  }

  function handleFolderClick(item) {
    // Toggle the expanded state in studio state.
    studio.toggleFolder(item.path);
    // Lazily fetch on first expand.
    if (!s.expanded[item.path] && !folderCache[item.path]) {
      fetchFolder(item.path);
    }
  }

  function handleFileClick(item) {
    var parts = item.path.split("/");
    var basename = parts[parts.length - 1];
    studio.openTab({
      id: "file:" + item.path,
      kind: "file",
      ref: item.path,
      title: basename,
    });
  }

  function handleRefresh() {
    // Clear folder cache and re-request root.
    setFolderCache({});
    rootRes.refetch && rootRes.refetch();
  }

  // Flatten the tree into a list of rows for rendering, respecting expansion.
  function ST_flattenItems(items, depth) {
    var rows = [];
    if (!items) return rows;
    items.forEach(function(item) {
      rows.push({ item: item, depth: depth });
      if (item.is_dir && s.expanded[item.path]) {
        var cached = folderCache[item.path];
        if (cached && cached.loaded) {
          var childRows = ST_flattenItems(cached.items, depth + 1);
          childRows.forEach(function(r) { rows.push(r); });
        } else if (cached && cached.loading) {
          rows.push({ spinner: true, path: item.path, depth: depth + 1 });
        }
      }
    });
    return rows;
  }

  var rootItems = (rootRes.data && rootRes.data.items) ? rootRes.data.items : [];
  var flatRows = ST_flattenItems(rootItems, 0);

  var chevStyle = {
    display: "inline-block",
    transition: "transform 0.15s",
    transform: s.filesOpen ? "rotate(0deg)" : "rotate(-90deg)",
    color: "var(--text-4)",
  };

  return (
    <div
      className="st-section"
      style={{ flex: "1 1 auto", minHeight: 0 }}
      data-testid="files-section"
    >
      {/* Section header */}
      <div
        className="st-section-h"
        data-testid="files-header"
        onClick={studio.toggleFiles}
      >
        <span style={chevStyle}>▾</span>
        Files
        <span style={{ flex: 1 }} />
        <button
          style={{
            width: 20,
            height: 20,
            display: "grid",
            placeItems: "center",
            borderRadius: 5,
            border: "none",
            background: "none",
            color: "var(--text-3)",
            fontSize: 14,
            cursor: "pointer",
            flexShrink: 0,
          }}
          title="Refresh"
          onClick={function(e) {
            e.stopPropagation();
            handleRefresh();
          }}
        >⟳</button>
        <button
          style={{
            width: 20,
            height: 20,
            display: "grid",
            placeItems: "center",
            borderRadius: 5,
            border: "none",
            background: s.showHidden ? "var(--accent-dim)" : "none",
            color: s.showHidden ? "var(--accent)" : "var(--text-3)",
            fontSize: 14,
            cursor: "pointer",
            flexShrink: 0,
          }}
          data-testid="hidden-toggle"
          title="Show hidden files"
          onClick={function(e) {
            e.stopPropagation();
            studio.toggleHidden();
          }}
        >⊘</button>
      </div>

      {/* File tree */}
      {s.filesOpen && (
        <div className="st-section-body" style={{ paddingBottom: 6 }}>
          {rootRes.loading && flatRows.length === 0 && (
            <div style={{ padding: "8px 12px", fontSize: 11, color: "var(--text-4)" }}>Loading…</div>
          )}
          {!rootRes.loading && flatRows.length === 0 && (
            <div style={{ padding: "8px 12px", fontSize: 11, color: "var(--text-4)" }}>No files.</div>
          )}
          {flatRows.map(function(row, idx) {
            if (row.spinner) {
              return (
                <div
                  key={"spin:" + row.path + ":" + idx}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    height: "var(--frow-h, 30px)",
                    paddingLeft: (row.depth * 16 + 8) + "px",
                    color: "var(--text-4)",
                    fontSize: 11,
                  }}
                >
                  ···
                </div>
              );
            }
            var item = row.item;
            var depth = row.depth;
            var tabId = "file:" + item.path;
            var isActive = s.activeTabId === tabId;
            var isDirty = false; // dirty state lives in studio.state.openTabs
            var openTabs = s.openTabs || [];
            for (var ti = 0; ti < openTabs.length; ti++) {
              if (openTabs[ti].id === tabId && openTabs[ti].dirty) {
                isDirty = true;
                break;
              }
            }
            var isExpanded = !!s.expanded[item.path];
            var iconColor = ST_fileIconColor(item);
            var iconName = ST_fileIconName(item);

            return (
              <div
                key={item.path}
                className="st-file-row"
                data-testid="file-row"
                onClick={function() {
                  if (item.is_dir) {
                    handleFolderClick(item);
                  } else {
                    handleFileClick(item);
                  }
                }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  height: "var(--frow-h, 30px)",
                  paddingLeft: (depth * 16 + 8) + "px",
                  paddingRight: 8,
                  cursor: "pointer",
                  background: isActive ? "var(--bg-active)" : "transparent",
                }}
              >
                {/* Chevron column */}
                <span style={{ width: 12, color: "var(--text-4)", fontSize: 9, textAlign: "center", flexShrink: 0 }}>
                  {item.is_dir ? (isExpanded ? "▾" : "▸") : ""}
                </span>
                {/* Icon column */}
                <span style={{ width: 15, display: "grid", placeItems: "center", flexShrink: 0, color: iconColor }}>
                  <Icon name={iconName} size={12} />
                </span>
                {/* Name */}
                <span style={{
                  flex: 1,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  fontSize: 12,
                  fontWeight: item.is_dir ? 500 : 400,
                  color: isActive ? "var(--text)" : "var(--text-2)",
                  marginLeft: 4,
                }}>
                  {item.name}
                </span>
                {/* Dirty dot */}
                {isDirty && (
                  <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--blue)", marginLeft: 4, flexShrink: 0 }} />
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// StudioSidebar — top-level component that replaces region-sidebar.
// Props: { wid, studio }  (studio = useStudioState(wid) bag)
// ---------------------------------------------------------------------------

function StudioSidebar({ wid, studio }) {
  return (
    <div
      style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}
      data-testid="studio-sidebar-inner"
    >
      <SessionsSection wid={wid} studio={studio} />
      <FilesTree wid={wid} studio={studio} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// No-build exports
// ---------------------------------------------------------------------------
window.StudioSidebar = StudioSidebar;
window.SessionsSection = SessionsSection;
window.FilesTree = FilesTree;
window.ST_SessionDeleteDialog = ST_SessionDeleteDialog;
window.ST_SessionRenameDialog = ST_SessionRenameDialog;
window.ST_sessionStatus = ST_sessionStatus;
window.ST_sessionKind = ST_sessionKind;
window.ST_sessionGlyph = ST_sessionGlyph;
