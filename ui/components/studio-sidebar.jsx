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
  if (item.origin === "collection") return "collection";
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
  if (item.origin === "collection") return "var(--accent)";
  if (item.is_dir) return "var(--text-3)";
  var ext = (item.name || "").split(".").pop().toLowerCase();
  if (ext === "py") return "var(--blue)";
  if (ext === "md" || ext === "txt" || ext === "rst") return "var(--text-2)";
  return "var(--text-3)";
}

// ---------------------------------------------------------------------------
// ST_onRowKey — keyboard activation for role="button" rows (a11y, FC5c).
// Returns an onKeyDown handler that fires `activate` on Enter or Space, so the
// clickable session / file rows are reachable and operable without a mouse.
// ---------------------------------------------------------------------------

function ST_onRowKey(activate) {
  return function (e) {
    if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
      e.preventDefault();
      activate();
    }
  };
}

// ---------------------------------------------------------------------------
// ST_pathBase / ST_pathDir / ST_pathJoin — workspace-relative path helpers.
// Paths are always POSIX "/"-separated (see FileEntry.path). basename returns
// the last segment; dirname returns the parent ("" for a top-level entry);
// join glues a dir + name, tolerating an empty dir (root) so it never emits a
// leading "/". Shared by the context-menu rename, the "… here" create actions,
// and the drag-to-move destination computation.
// ---------------------------------------------------------------------------

function ST_pathBase(path) {
  var p = String(path || "");
  var i = p.lastIndexOf("/");
  return i >= 0 ? p.slice(i + 1) : p;
}

function ST_pathDir(path) {
  var p = String(path || "");
  var i = p.lastIndexOf("/");
  return i >= 0 ? p.slice(0, i) : "";
}

function ST_pathJoin(dir, name) {
  var d = String(dir || "");
  var n = String(name || "");
  return d ? d + "/" + n : n;
}

// ---------------------------------------------------------------------------
// ST_triggerDownload — kick off a browser download of a workspace file.
// Mirrors the center editor's <a href=…/files/download> affordance (same URL)
// but drives it programmatically so the Files-tree context menu's "Download"
// action can reuse the exact same flow without a persistent anchor.
// ---------------------------------------------------------------------------

function ST_triggerDownload(wid, path) {
  var url =
    "/v1/workspaces/" +
    encodeURIComponent(wid) +
    "/files/download?path=" +
    encodeURIComponent(path);
  var a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noreferrer noopener";
  a.download = ST_pathBase(path);
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ---------------------------------------------------------------------------
// ST_FileContextMenu — small absolutely-positioned right-click menu for a
// file / folder row in the Files tree. The action set differs by kind:
//   file   → Open · Download · Rename · Delete
//   folder → New file here · New folder here · Upload here · Rename · Delete
// It closes on outside-click (mousedown) or Escape, and each item fires
// onAction(<key>, item) then onClose(). Positioned at the cursor via
// position:fixed (clientX/clientY). testids: file-context-menu + ctx-<key>.
// ---------------------------------------------------------------------------

function ST_FileContextMenu({ menu, onAction, onClose }) {
  React.useEffect(function () {
    function onDocDown(e) {
      // A click INSIDE the menu is handled by the item's own onClick; anything
      // else dismisses. Guard on the container via a data attribute lookup.
      var el = e.target;
      while (el) {
        if (el.getAttribute && el.getAttribute("data-testid") === "file-context-menu") {
          return;
        }
        el = el.parentNode;
      }
      onClose();
    }
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
    }
    document.addEventListener("mousedown", onDocDown, true);
    document.addEventListener("keydown", onKey, true);
    return function () {
      document.removeEventListener("mousedown", onDocDown, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [onClose]);

  if (!menu) return null;
  var item = menu.item;
  var isDir = !!item.is_dir;
  var actions = isDir
    ? [
        { key: "new-file", label: "New file here", icon: "file" },
        { key: "new-folder", label: "New folder here", icon: "box" },
        { key: "upload", label: "Upload here", icon: "paperclip" },
        { key: "rename", label: "Rename", icon: "edit" },
        { key: "delete", label: "Delete", icon: "trash", danger: true },
      ]
    : [
        { key: "open", label: "Open", icon: "doc" },
        { key: "download", label: "Download", icon: "external" },
        { key: "rename", label: "Rename", icon: "edit" },
        { key: "delete", label: "Delete", icon: "trash", danger: true },
      ];
  // Mounted-collection roots get an extra Detach entry (Task 11). The
  // "apply to collection" entry (Task 12) is deliberately NOT added here yet.
  if (menu.item && menu.item.origin === "collection") {
    actions = actions.concat([
      { key: "detach", label: "Detach collection", icon: "trash", danger: true },
    ]);
  }

  // Keep the menu inside the viewport: nudge left/up when it would overflow.
  var MENU_W = 176;
  var estH = actions.length * 30 + 8;
  var vw = typeof window !== "undefined" ? window.innerWidth : 1024;
  var vh = typeof window !== "undefined" ? window.innerHeight : 768;
  var left = Math.min(menu.x, Math.max(8, vw - MENU_W - 8));
  var top = Math.min(menu.y, Math.max(8, vh - estH - 8));

  return (
    <div
      data-testid="file-context-menu"
      role="menu"
      onContextMenu={function (e) { e.preventDefault(); }}
      style={{
        position: "fixed",
        left: left,
        top: top,
        zIndex: 1000,
        minWidth: MENU_W,
        background: "var(--bg-2)",
        border: "1px solid var(--border)",
        borderRadius: 8,
        boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
        padding: 4,
        fontSize: 12.5,
      }}
    >
      {actions.map(function (a) {
        return (
          <button
            key={a.key}
            type="button"
            role="menuitem"
            data-testid={"ctx-" + a.key}
            onClick={function (e) {
              e.preventDefault();
              e.stopPropagation();
              onAction(a.key, item);
              onClose();
            }}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              width: "100%",
              textAlign: "left",
              padding: "6px 10px",
              borderRadius: 6,
              border: "none",
              background: "none",
              cursor: "pointer",
              color: a.danger ? "var(--red)" : "var(--text)",
              fontSize: 12.5,
              fontFamily: "inherit",
            }}
            onMouseEnter={function (e) { e.currentTarget.style.background = "var(--bg-active)"; }}
            onMouseLeave={function (e) { e.currentTarget.style.background = "none"; }}
          >
            <span style={{ width: 14, display: "grid", placeItems: "center", flexShrink: 0 }}>
              <Icon name={a.icon} size={12} />
            </span>
            {a.label}
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// NewSessionForm — inline modal-style form for creating a session.
// Renders as a positioned overlay inside the sessions section.
// POST /v1/workspaces/{wid}/sessions with the SessionCreateBody shape:
//   { binding: { kind, agent_id? | graph_id? }, auto_start, initial_instructions? }
// ---------------------------------------------------------------------------

function NewSessionForm({ wid, onClose, onCreated }) {
  // Thin wrapper around the shared create-session form (FD2). The inline
  // variant renders the positioned overlay chrome (data-testid new-session-form)
  // + the optional `name` field (#22) and now also supports a graph's
  // Begin.input_schema, inherited from the shared superset.
  return (
    <window.SharedNewSessionForm
      variant="inline"
      wid={wid}
      onCancel={onClose}
      onCreated={onCreated}
    />
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
                role="button"
                tabIndex={0}
                onClick={function() { openSession(session); }}
                onKeyDown={ST_onRowKey(function() { openSession(session); })}
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
                  <span className="st-pill" style={{
                    fontSize: 9,
                    padding: "1px 5px",
                    background: "var(--amber-dim)",
                    color: "var(--amber)",
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
// ST_MountCollectionModal — mount an existing collection into this workspace.
//   POST /v1/workspaces/{wid}/mounts { collection_id, dest? } -> MountEntry.
//   The collection picker is a <select> sourced from GET /collections?limit=200
//   (mirrors WS_NewWorkspaceModal's template picker in workspaces.jsx). `dest`
//   is optional — the backend defaults it from the collection when omitted.
//   On success: toast, close, and onMounted() so the caller (FilesTree's
//   handleRefresh) reloads the tree and shows the new mounted directory.
// ---------------------------------------------------------------------------

function ST_MountCollectionModal({ wid, onClose, onMounted, pushToast }) {
  var api = window.primerApi;
  var collections = api.useResource(
    "collections:list",
    function (signal) { return api.apiFetch("GET", "/collections?limit=200", null, { signal: signal }); },
    {}
  );
  var items = (collections.data && collections.data.items) || [];
  var [collectionId, setCollectionId] = React.useState("");
  var [dest, setDest] = React.useState("");

  var create = api.useMutation(
    function (body) { return api.apiFetch("POST", "/workspaces/" + encodeURIComponent(wid) + "/mounts", body); },
    {
      onSuccess: function (row) {
        onClose();
        pushToast && pushToast({ kind: "success", title: "Collection mounted", detail: row && row.dest });
        onMounted && onMounted();
      },
      onError: function (err) {
        pushToast && pushToast({
          kind: "error",
          title: "Mount failed",
          detail: err && (err.detail || err.title),
          requestId: err && err.requestId,
        });
      },
    }
  );

  function onMount() {
    if (!collectionId) return;
    var body = { collection_id: collectionId };
    if (dest.trim()) body.dest = dest.trim();
    create.mutate(body);
  }

  return (
    <Modal
      title="Mount collection"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={create.loading}>Cancel</Btn>
          <Btn
            kind="primary"
            disabled={!collectionId || create.loading}
            onClick={onMount}
            data-testid="mount-collection-submit"
          >
            {create.loading ? "Mounting…" : "Mount"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">Collection</label>
        <select
          className="select mono"
          style={{ width: "100%" }}
          data-testid="mount-collection-select"
          value={collectionId}
          onChange={function (e) { setCollectionId(e.target.value); }}
        >
          <option value="">Select a collection…</option>
          {items.map(function (c) {
            return <option key={c.id} value={c.id}>{c.description || c.id}</option>;
          })}
        </select>
      </div>
      <div className="field">
        <label className="field-label">Directory name <span className="hint">optional — defaults to the collection name</span></label>
        <input
          className="input mono"
          style={{ width: "100%" }}
          value={dest}
          onChange={function (e) { setDest(e.target.value); }}
          placeholder="e.g. research-notes"
        />
      </div>
    </Modal>
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

  // Shared mounted-collections resource: one GET /mounts backs both the
  // dirty-dot (Task 11, below) and the "apply to collection" action
  // (Task 12) — both need to resolve a tree item's dest path to its
  // mount_id / dirty flag. `dirty` is computed server-side (list_mounts).
  var mountsRes = useResource(
    "studio-mounts:" + wid,
    function (signal) {
      return apiFetch(
        "GET",
        "/workspaces/" + encodeURIComponent(wid) + "/mounts",
        null,
        { signal: signal }
      );
    },
    {}
  );
  var mountsByDest = {};
  ((mountsRes.data && mountsRes.data.mounts) || []).forEach(function (m) {
    mountsByDest[m.dest] = m;
  });

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

  function fetchFolder(path, force) {
    // Only fetch once per path (or if not yet started); force bypasses the
    // once-per-path guard so handleRefresh can reload an already-cached folder.
    if (!force && folderCache[path]) return;
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
    // Re-fetch folders that are currently expanded so their children reappear
    // after the cache wipe — otherwise an expanded subfolder unrelated to the
    // change renders blank until the user collapses + re-expands it. force=true
    // is required because fetchFolder's guard still sees the pre-clear cache
    // through its render-time closure.
    var expanded = s.expanded || {};
    Object.keys(expanded).forEach(function(p) {
      if (expanded[p]) fetchFolder(p, true);
    });
  }

  // -- File-action wiring (New file / Upload / New folder) -----------------
  // The backend already supports all three; these buttons are UI-only wiring.
  //   New file   → PUT  /files?path=<rel>  { content:"", encoding:"text" }  (no etag = create)
  //   Upload     → PUT  /files?path=<rel>  { content:<base64>, encoding:"base64" }
  //   New folder → POST /files/dir?path=<rel>  (path is a QUERY param)
  // Target dir: root-relative by default. FilesTree tracks `s.expanded` (a map
  // of expanded folders) but no single "selected" folder, so we do NOT invent
  // extra state — the prompt lets the user type `sub/dir/name` for a subpath.
  var uploadInputRef = React.useRef(null);
  // Which directory a hidden-input upload lands in. "" = workspace root (the
  // header Upload button); the context-menu "Upload here" sets it to a folder
  // path before clicking the SAME hidden input, and handleUploadChange reads +
  // resets it. A ref (not state) so it's read synchronously in the change
  // handler without a re-render race.
  var uploadTargetRef = React.useRef("");
  // Right-click context menu: { item, x, y } or null. Drop-target highlight:
  // the folder path (or "." for the tree root) currently under a drag, or null.
  var [ctxMenu, setCtxMenu] = React.useState(null);
  var [dropTarget, setDropTarget] = React.useState(null);
  // "Mount collection" modal — see ST_MountCollectionModal below.
  var [mountOpen, setMountOpen] = React.useState(false);
  var pushToast = studio.pushToast || (window.primerApi && window.primerApi.toastPush) || null;
  // promptDialog is published as a bare window global by shared.jsx; keep a
  // primerApi fallback so it resolves regardless of how it was exposed.
  var promptDialog = (window.primerApi && window.primerApi.promptDialog) || window.promptDialog;

  // Shared style for the section-header icon buttons (matches the refresh btn).
  var ST_HDR_BTN = {
    width: 20,
    height: 20,
    display: "grid",
    placeItems: "center",
    borderRadius: 5,
    border: "none",
    background: "none",
    color: "var(--text-3)",
    cursor: "pointer",
    flexShrink: 0,
    padding: 0,
  };

  function ST_errDetail(err, fallback) {
    return (err && (err.detail || err.message)) || fallback;
  }

  // Open a freshly-created file in the center editor. Mirrors handleFileClick's
  // tab shape, but seeds mode:"edit" so an empty new file is immediately typable.
  function ST_openNewFileTab(rel) {
    var parts = rel.split("/");
    var basename = parts[parts.length - 1];
    studio.openTab({
      id: "file:" + rel,
      kind: "file",
      ref: rel,
      title: basename,
      mode: "edit",
    });
  }

  async function handleNewFile() {
    var raw = await promptDialog({
      title: "New file",
      label: "File name",
      message: "File name",
      placeholder: "notes.md or sub/dir/file.py",
    });
    if (raw == null) return;
    var name = String(raw).trim();
    if (!name) return;
    try {
      await apiFetch(
        "PUT",
        "/workspaces/" + encodeURIComponent(wid) + "/files?path=" + encodeURIComponent(name),
        { content: "", encoding: "text" }
      );
      handleRefresh();
      ST_openNewFileTab(name);
      pushToast && pushToast({ kind: "success", title: "File created", detail: name });
    } catch (err) {
      pushToast && pushToast({
        kind: "error",
        title: "Create failed",
        detail: ST_errDetail(err, "Create failed"),
        requestId: err && err.requestId,
      });
    }
  }

  async function handleNewFolder() {
    var raw = await promptDialog({
      title: "New folder",
      label: "Folder name",
      message: "Folder name",
      placeholder: "src or docs/notes",
    });
    if (raw == null) return;
    var name = String(raw).trim();
    if (!name) return;
    try {
      await apiFetch(
        "POST",
        "/workspaces/" + encodeURIComponent(wid) + "/files/dir?path=" + encodeURIComponent(name)
      );
      handleRefresh();
      pushToast && pushToast({ kind: "success", title: "Folder created", detail: name });
    } catch (err) {
      pushToast && pushToast({
        kind: "error",
        title: "Create failed",
        detail: ST_errDetail(err, "Create failed"),
        requestId: err && err.requestId,
      });
    }
  }

  // -- Delete (file or folder) ---------------------------------------------
  // DELETE /files?path=<rel>  (path is a QUERY param). The backend removes a
  // regular file with unlink() and a directory recursively via shutil.rmtree
  // when recursive=true (primer/workspace/local/workspace.py delete_file), so
  // Delete is offered on BOTH files and folders; folders pass recursive=true
  // and the confirm warns that the contents go with it. On success the tree is
  // refetched, any open editor tab for the path is closed, and a toast fires.
  async function handleDelete(item) {
    var isDir = !!item.is_dir;
    var name = item.name || item.path;
    // Bare global from shared.jsx (Object.assign(window, {confirmDialog})) — the
    // themed shared Modal dialog, not the native browser prompt. Same call
    // shape as triggers.jsx.
    var ok = await confirmDialog({
      title: isDir ? "Delete folder" : "Delete file",
      message: isDir
        ? "Delete \"" + name + "\" and everything inside it? This cannot be undone."
        : "Delete \"" + name + "\"? This cannot be undone.",
      danger: true,
    });
    if (!ok) return;
    var url = "/workspaces/" + encodeURIComponent(wid) + "/files?path=" + encodeURIComponent(item.path);
    if (isDir) url += "&recursive=true";
    try {
      await apiFetch("DELETE", url);
      handleRefresh();
      // Close the deleted path's editor tab AND, for a folder, every open tab
      // for a file *under* it: the backend rmtree removed those files, and a
      // surviving tab could silently re-create one on save (write_file re-mkdirs
      // parents and bypasses the etag check for a now-missing file). closeTab is
      // a functional state update, so closing several in sequence is safe. The
      // trailing "/" guards against sibling-prefix matches (deleting "foo" must
      // not touch a tab for "foobar").
      if (studio.closeTab) {
        var selfTabId = "file:" + item.path;
        var childTabPrefix = selfTabId + "/";
        var openTabs = s.openTabs || [];
        for (var ti = 0; ti < openTabs.length; ti++) {
          var tid = openTabs[ti].id;
          if (tid === selfTabId || (isDir && tid.indexOf(childTabPrefix) === 0)) {
            studio.closeTab(tid);
          }
        }
      }
      pushToast && pushToast({
        kind: "success",
        title: isDir ? "Folder deleted" : "File deleted",
        detail: name,
      });
    } catch (err) {
      pushToast && pushToast({
        kind: "error",
        title: "Delete failed",
        detail: ST_errDetail(err, "Delete failed"),
        requestId: err && err.requestId,
      });
    }
  }

  // -- Detach a mounted collection ------------------------------------------
  // DELETE /workspaces/{wid}/mounts/{mount_id}[?force=true]. A clean copy
  // (no local edits since mount/last apply) 204s straight away. A diverged
  // copy 409s with {modified, changed} — that's caught below and turned into
  // a confirm dialog, then retried with force=true. Either way the upstream
  // collection itself is never touched, only the workspace's local copy.
  async function handleDetach(item) {
    var mount = mountsByDest[item.path];
    if (!mount) return;
    function doDetach(force) {
      return apiFetch(
        "DELETE",
        "/workspaces/" + encodeURIComponent(wid) +
          "/mounts/" + encodeURIComponent(mount.mount_id) + (force ? "?force=true" : "")
      );
    }
    try {
      await doDetach(false);
    } catch (err) {
      if (err && err.status === 409) {
        var ok = await confirmDialog({
          title: "Detach collection",
          message: "\"" + item.path + "\" has local changes that haven't been applied to the collection. Detach anyway? The upstream collection is unchanged.",
          danger: true,
          confirmLabel: "Detach",
        });
        if (!ok) return;
        try {
          await doDetach(true);
        } catch (err2) {
          pushToast && pushToast({
            kind: "error",
            title: "Detach failed",
            detail: ST_errDetail(err2, "Detach failed"),
            requestId: err2 && err2.requestId,
          });
          return;
        }
      } else {
        pushToast && pushToast({
          kind: "error",
          title: "Detach failed",
          detail: ST_errDetail(err, "Detach failed"),
          requestId: err && err.requestId,
        });
        return;
      }
    }
    handleRefresh();
    mountsRes.refetch && mountsRes.refetch();
    pushToast && pushToast({ kind: "success", title: "Collection detached", detail: item.path });
  }

  function ST_readAsBase64(file) {
    // Resolve the raw base64 payload (the data: URL minus its "data:...;base64,"
    // prefix) so it can be PUT with encoding:"base64".
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        var result = String(reader.result || "");
        var comma = result.indexOf(",");
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = function () { reject(reader.error || new Error("read failed")); };
      reader.readAsDataURL(file);
    });
  }

  // Shared single-file base64 PUT, targeted at a directory. `dir` "" = root.
  // The one place the base64 upload payload is written — reused by the hidden
  // <input> flow (header + "Upload here") AND the drag-drop-from-OS flow.
  async function ST_putUpload(dir, file) {
    var b64 = await ST_readAsBase64(file);
    var rel = dir ? dir + "/" + file.name : file.name;
    await apiFetch(
      "PUT",
      "/workspaces/" + encodeURIComponent(wid) + "/files?path=" + encodeURIComponent(rel),
      { content: b64, encoding: "base64" }
    );
  }

  // Upload a batch of File objects into `dir`, then refresh + toast. Used by
  // both the file-picker change handler and the drag-drop-from-OS handler.
  async function ST_uploadInto(dir, fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    var okCount = 0;
    var errors = [];
    for (var i = 0; i < files.length; i++) {
      var file = files[i];
      try {
        await ST_putUpload(dir, file);
        okCount += 1;
      } catch (err) {
        errors.push(file.name + ": " + ST_errDetail(err, "upload failed"));
      }
    }
    handleRefresh();
    if (okCount > 0) {
      pushToast && pushToast({
        kind: "success",
        title: "Uploaded " + okCount + " file" + (okCount === 1 ? "" : "s"),
        detail: dir ? "into " + dir : undefined,
      });
    }
    if (errors.length) {
      pushToast && pushToast({
        kind: "error",
        title: "Upload failed",
        detail: errors.join("; "),
      });
    }
  }

  function handleUploadClick() {
    // Header Upload button → target the workspace root.
    uploadTargetRef.current = "";
    if (uploadInputRef.current) uploadInputRef.current.click();
  }

  function handleUploadIn(dir) {
    // Context-menu "Upload here" → target this folder, reusing the same input.
    uploadTargetRef.current = dir || "";
    if (uploadInputRef.current) uploadInputRef.current.click();
  }

  async function handleUploadChange(e) {
    var input = e.target;
    var files = (input && input.files) ? Array.prototype.slice.call(input.files) : [];
    var dir = uploadTargetRef.current || "";
    uploadTargetRef.current = "";
    // Reset so re-picking the SAME file fires another change event.
    if (input) input.value = "";
    await ST_uploadInto(dir, files);
  }

  // -- Targeted create actions (context-menu "… here") --------------------
  // Same endpoints as the header buttons, but rooted at a folder path instead
  // of the workspace root.
  async function handleNewFileIn(dir) {
    var raw = await promptDialog({
      title: "New file",
      label: "File name",
      message: "New file in " + (dir || "root"),
      placeholder: "notes.md",
    });
    if (raw == null) return;
    var name = String(raw).trim();
    if (!name) return;
    var rel = ST_pathJoin(dir, name);
    try {
      await apiFetch(
        "PUT",
        "/workspaces/" + encodeURIComponent(wid) + "/files?path=" + encodeURIComponent(rel),
        { content: "", encoding: "text" }
      );
      handleRefresh();
      ST_openNewFileTab(rel);
      pushToast && pushToast({ kind: "success", title: "File created", detail: rel });
    } catch (err) {
      pushToast && pushToast({
        kind: "error",
        title: "Create failed",
        detail: ST_errDetail(err, "Create failed"),
        requestId: err && err.requestId,
      });
    }
  }

  async function handleNewFolderIn(dir) {
    var raw = await promptDialog({
      title: "New folder",
      label: "Folder name",
      message: "New folder in " + (dir || "root"),
      placeholder: "src",
    });
    if (raw == null) return;
    var name = String(raw).trim();
    if (!name) return;
    var rel = ST_pathJoin(dir, name);
    try {
      await apiFetch(
        "POST",
        "/workspaces/" + encodeURIComponent(wid) + "/files/dir?path=" + encodeURIComponent(rel)
      );
      handleRefresh();
      pushToast && pushToast({ kind: "success", title: "Folder created", detail: rel });
    } catch (err) {
      pushToast && pushToast({
        kind: "error",
        title: "Create failed",
        detail: ST_errDetail(err, "Create failed"),
        requestId: err && err.requestId,
      });
    }
  }

  // -- Rename / move tab remap --------------------------------------------
  // After a rename/move, repoint any OPEN editor tab at the file's new path so
  // a surviving tab doesn't go stale (a stale tab could silently re-create the
  // old file on save). For a file we remap its exact tab; for a folder we remap
  // every descendant file tab (prefix "file:<old>/"), keeping their basenames.
  function ST_remapTabsForMove(oldPath, newPath, isDir) {
    if (!studio.renameTab) return;
    var openTabs = s.openTabs || [];
    var selfId = "file:" + oldPath;
    var childPrefix = "file:" + oldPath + "/";
    for (var i = 0; i < openTabs.length; i++) {
      var tid = openTabs[i].id;
      if (!isDir && tid === selfId) {
        studio.renameTab(tid, { id: "file:" + newPath, ref: newPath, title: ST_pathBase(newPath) });
      } else if (isDir && tid.indexOf(childPrefix) === 0) {
        var oldFilePath = tid.slice("file:".length); // oldPath + "/sub/x"
        var suffix = oldFilePath.slice(oldPath.length); // "/sub/x"
        var newFilePath = newPath + suffix;
        studio.renameTab(tid, { id: "file:" + newFilePath, ref: newFilePath });
      }
    }
  }

  // -- Rename (context-menu action) ----------------------------------------
  // Prompt for a new basename (default = current), POST the move within the
  // same parent dir, remap open tabs, refresh, toast. No move endpoint existed
  // before this feature (see POST /files/move); the backend rejects a collision
  // / escape, surfaced here as an error toast.
  async function handleRename(item) {
    var isDir = !!item.is_dir;
    var oldPath = item.path;
    var base = ST_pathBase(oldPath);
    var dir = ST_pathDir(oldPath);
    var raw = await promptDialog({
      title: isDir ? "Rename folder" : "Rename file",
      label: "New name",
      message: "Rename \"" + base + "\" to:",
      defaultValue: base,
      placeholder: base,
    });
    if (raw == null) return;
    var name = String(raw).trim();
    if (!name || name === base) return;
    var dst = ST_pathJoin(dir, name);
    try {
      await apiFetch(
        "POST",
        "/workspaces/" + encodeURIComponent(wid) + "/files/move?src=" +
          encodeURIComponent(oldPath) + "&dst=" + encodeURIComponent(dst)
      );
      ST_remapTabsForMove(oldPath, dst, isDir);
      handleRefresh();
      pushToast && pushToast({
        kind: "success",
        title: isDir ? "Folder renamed" : "File renamed",
        detail: name,
      });
    } catch (err) {
      pushToast && pushToast({
        kind: "error",
        title: "Rename failed",
        detail: ST_errDetail(err, "Rename failed"),
        requestId: err && err.requestId,
      });
    }
  }

  // -- Drag-to-move --------------------------------------------------------
  // Move `srcPath` into `folderPath` via the move endpoint. Guards no-ops:
  // dropping onto itself, into a descendant of itself, or into the folder it
  // already lives in.
  async function doMove(srcPath, isDir, folderPath) {
    if (folderPath === srcPath || folderPath.indexOf(srcPath + "/") === 0) return; // self/descendant
    if (ST_pathDir(srcPath) === folderPath) return; // already here
    var dst = ST_pathJoin(folderPath, ST_pathBase(srcPath));
    try {
      await apiFetch(
        "POST",
        "/workspaces/" + encodeURIComponent(wid) + "/files/move?src=" +
          encodeURIComponent(srcPath) + "&dst=" + encodeURIComponent(dst)
      );
      ST_remapTabsForMove(srcPath, dst, isDir);
      handleRefresh();
      pushToast && pushToast({
        kind: "success",
        title: "Moved",
        detail: ST_pathBase(srcPath) + " → " + folderPath,
      });
    } catch (err) {
      pushToast && pushToast({
        kind: "error",
        title: "Move failed",
        detail: ST_errDetail(err, "Move failed"),
        requestId: err && err.requestId,
      });
    }
  }

  // -- Context-menu action dispatch ----------------------------------------
  function handleCtxAction(action, item) {
    if (action === "open") handleFileClick(item);
    else if (action === "download") ST_triggerDownload(wid, item.path);
    else if (action === "rename") handleRename(item);
    else if (action === "delete") handleDelete(item);
    else if (action === "detach") handleDetach(item);
    else if (action === "new-file") handleNewFileIn(item.path);
    else if (action === "new-folder") handleNewFolderIn(item.path);
    else if (action === "upload") handleUploadIn(item.path);
  }

  // -- Drag helpers (rows are drag SOURCES; folders + root are drop TARGETS) --
  // dragstart stamps the source path/kind onto the DataTransfer so a drop can
  // tell an internal row-move from an external OS-file upload.
  function ST_dragStartRow(item, e) {
    if (!e.dataTransfer) return;
    e.dataTransfer.setData(
      "application/x-primer-file",
      JSON.stringify({ path: item.path, is_dir: !!item.is_dir })
    );
    e.dataTransfer.setData("text/plain", item.path);
    e.dataTransfer.effectAllowed = "move";
  }

  // True when the drag carries OS files (upload) vs an internal row (move).
  function ST_isFileDrag(e) {
    var types = e.dataTransfer ? e.dataTransfer.types : null;
    if (!types) return false;
    if (types.indexOf) return types.indexOf("Files") !== -1;
    if (types.contains) return types.contains("Files");
    return false;
  }

  function ST_dragOverFolder(item, e) {
    if (!item.is_dir) return; // only folders accept drops
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = ST_isFileDrag(e) ? "copy" : "move";
    setDropTarget(item.path);
  }

  function ST_dragLeaveFolder(item) {
    setDropTarget(function (cur) { return cur === item.path ? null : cur; });
  }

  async function ST_dropOnFolder(item, e) {
    if (!item.is_dir) return;
    e.preventDefault();
    e.stopPropagation();
    setDropTarget(null);
    var dt = e.dataTransfer;
    if (dt && dt.files && dt.files.length) {
      // OS files → upload INTO this folder.
      await ST_uploadInto(item.path, dt.files);
      return;
    }
    // Internal row → move INTO this folder.
    var raw = dt ? dt.getData("application/x-primer-file") : "";
    if (!raw) return;
    var src = null;
    try { src = JSON.parse(raw); } catch (_e) { return; }
    if (!src || !src.path) return;
    await doMove(src.path, !!src.is_dir, item.path);
  }

  // Tree-root drop target ("." sentinel): OS-file drops only (upload to root).
  function ST_dragOverRoot(e) {
    if (!ST_isFileDrag(e)) return; // internal moves need a folder target
    e.preventDefault();
    setDropTarget(".");
  }
  function ST_dragLeaveRoot() {
    setDropTarget(function (cur) { return cur === "." ? null : cur; });
  }
  async function ST_dropOnRoot(e) {
    var dt = e.dataTransfer;
    if (!(dt && dt.files && dt.files.length)) return;
    e.preventDefault();
    setDropTarget(null);
    await ST_uploadInto("", dt.files);
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
        {/* New file / Upload / New folder — UI wiring over existing backend
            endpoints. Each stops propagation so it doesn't toggle the section. */}
        <button
          type="button"
          style={ST_HDR_BTN}
          title="New file"
          aria-label="New file"
          data-testid="files-new-file"
          onClick={function(e) {
            e.stopPropagation();
            handleNewFile();
          }}
        ><Icon name="file" size={13} /></button>
        <button
          type="button"
          style={ST_HDR_BTN}
          title="Upload files"
          aria-label="Upload files"
          data-testid="files-upload"
          onClick={function(e) {
            e.stopPropagation();
            handleUploadClick();
          }}
        ><Icon name="paperclip" size={13} /></button>
        {/* Hidden multi-file input driven by the Upload button. */}
        <input
          ref={uploadInputRef}
          type="file"
          multiple
          data-testid="files-upload-input"
          style={{ display: "none" }}
          onClick={function(e) { e.stopPropagation(); }}
          onChange={handleUploadChange}
        />
        <button
          type="button"
          style={ST_HDR_BTN}
          title="New folder"
          aria-label="New folder"
          data-testid="files-new-folder"
          onClick={function(e) {
            e.stopPropagation();
            handleNewFolder();
          }}
        ><Icon name="box" size={13} /></button>
        <button
          type="button"
          style={ST_HDR_BTN}
          title="Mount collection"
          aria-label="Mount collection"
          data-testid="files-mount-collection"
          onClick={function(e) {
            e.stopPropagation();
            setMountOpen(true);
          }}
        ><Icon name="collection" size={13} /></button>
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
        <div
          className="st-section-body"
          data-testid="files-tree-body"
          onDragOver={ST_dragOverRoot}
          onDragLeave={ST_dragLeaveRoot}
          onDrop={ST_dropOnRoot}
          style={{
            paddingBottom: 6,
            outline: dropTarget === "." ? "1px solid var(--accent)" : "none",
            outlineOffset: "-2px",
            background: dropTarget === "." ? "var(--accent-dim)" : undefined,
          }}
        >
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
            // Highlight a folder row while a drag hovers it (upload or move).
            var isDropTarget = item.is_dir && dropTarget === item.path;

            return (
              <div
                key={item.path}
                className="st-file-row"
                data-testid="file-row"
                role="button"
                tabIndex={0}
                draggable={true}
                onClick={function() {
                  if (item.is_dir) {
                    handleFolderClick(item);
                  } else {
                    handleFileClick(item);
                  }
                }}
                onKeyDown={ST_onRowKey(function() {
                  if (item.is_dir) {
                    handleFolderClick(item);
                  } else {
                    handleFileClick(item);
                  }
                })}
                onContextMenu={function(e) {
                  // Open the right-click menu; preventDefault suppresses the
                  // native browser menu, stopPropagation keeps the row's
                  // open/toggle click from firing.
                  e.preventDefault();
                  e.stopPropagation();
                  setCtxMenu({ item: item, x: e.clientX, y: e.clientY });
                }}
                onDragStart={function(e) { ST_dragStartRow(item, e); }}
                onDragOver={function(e) { ST_dragOverFolder(item, e); }}
                onDragLeave={function() { ST_dragLeaveFolder(item); }}
                onDrop={function(e) { ST_dropOnFolder(item, e); }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  height: "var(--frow-h, 30px)",
                  paddingLeft: (depth * 16 + 8) + "px",
                  paddingRight: 8,
                  cursor: "pointer",
                  background: isDropTarget
                    ? "var(--accent-dim)"
                    : (isActive ? "var(--bg-active)" : "transparent"),
                  outline: isDropTarget ? "1px solid var(--accent)" : "none",
                  outlineOffset: "-1px",
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
                {/* Collection dirty-dot: distinct testid/color from the edit
                    dirty-dot above — flags a mount whose local copy has
                    diverged from its base snapshot (dirty flag comes from
                    GET /mounts, looked up here via mountsByDest). */}
                {item.origin === "collection" && mountsByDest[item.path] && mountsByDest[item.path].dirty && (
                  <span
                    data-testid="collection-dirty-dot"
                    title="Unsynced local changes"
                    style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--warn, #d9822b)", marginLeft: 4, flexShrink: 0 }}
                  />
                )}
                {/* Hover/focus-revealed row action (delete). stopPropagation keeps
                    the row's open-file / toggle-folder click from firing. Folders
                    delete recursively (backend rmtree via recursive=true). */}
                <span className="st-row-actions">
                  <button
                    type="button"
                    className="st-row-action is-danger"
                    data-testid={item.is_dir ? "folder-delete" : "file-delete"}
                    title={item.is_dir ? "Delete folder" : "Delete file"}
                    aria-label={item.is_dir ? "Delete folder" : "Delete file"}
                    onClick={function(e) { e.stopPropagation(); handleDelete(item); }}
                    onKeyDown={function(e) {
                      // The row div (role="button") owns an onKeyDown that
                      // preventDefaults Enter/Space to open the file / toggle the
                      // folder; without stopping the bubble here it would hijack a
                      // keyboard user's activation of this trash button (a11y).
                      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
                        e.preventDefault();
                        e.stopPropagation();
                        handleDelete(item);
                      }
                    }}
                  >
                    <Icon name="trash" size={12} />
                  </button>
                </span>
              </div>
            );
          })}
        </div>
      )}

      {/* Right-click context menu (file/folder actions). Rendered last so its
          position:fixed overlay sits above the tree; closes on outside-click
          or Escape (handled inside ST_FileContextMenu). */}
      {ctxMenu && (
        <ST_FileContextMenu
          menu={ctxMenu}
          onAction={handleCtxAction}
          onClose={function() { setCtxMenu(null); }}
        />
      )}

      {/* "Mount collection" modal, opened from the header action button. */}
      {mountOpen && (
        <ST_MountCollectionModal
          wid={wid}
          pushToast={pushToast}
          onClose={function() { setMountOpen(false); }}
          onMounted={function () { handleRefresh(); mountsRes.refetch && mountsRes.refetch(); }}
        />
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
window.ST_FileContextMenu = ST_FileContextMenu;
window.ST_SessionDeleteDialog = ST_SessionDeleteDialog;
window.ST_SessionRenameDialog = ST_SessionRenameDialog;
window.ST_MountCollectionModal = ST_MountCollectionModal;
window.ST_sessionStatus = ST_sessionStatus;
window.ST_sessionKind = ST_sessionKind;
window.ST_sessionGlyph = ST_sessionGlyph;
