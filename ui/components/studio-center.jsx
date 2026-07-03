/* global React, Icon, Btn, Banner */
// StudioCenter — center region of the Studio IDE shell (PR-B / B3).
//
// Replaces the ST_RegionPlaceholder at data-testid="region-center" inside
// the shell's data-testid="studio-center" column. Renders:
//   CenterTabs        — the tab bar over studio.state.openTabs (glyph/title,
//                        dirty dot, close ×; horizontal overflow; empty state).
//   the active panel  — chosen by the active tab's kind:
//     session(agent) → SessionAgentPanel  (header + controls + SessionLiveStream)
//     session(graph) → SessionGraphPanel  (header + SD_GraphRunView)
//     file           → FilePanel          (preview/edit toggle + Save + 412 flow)
//
// REUSE (do NOT rebuild): the agent transcript and the graph run-view are the
// production components from session-detail.jsx, reached across files as the
// no-build window globals:
//   window.SessionLiveStream  ({ sid, wid, session, pushToast })
//   window.SD_GraphRunView    ({ gid, rid, wid, session, pushToast })
// Markdown preview reuses window.renderMarkdown; code highlight reuses
// window.primerVendor.highlightPython.
//
// REUSE: the per-session control mutations (pause/resume/steer/cancel) are the
// shared window.useSessionControls hook (ui/components/use-session-controls.jsx,
// FD1b) — ST_SessionControls no longer re-implements them. Same workspace-scoped
// endpoints (POST .../sessions/{sid}/{pause|resume|cancel|steer}).
//
// No-build rules: top-level declarations use `var`; helpers prefixed ST_;
// every exported symbol is assigned to window.X at the bottom.

// ---------------------------------------------------------------------------
// ST_basename — last path segment, for the file breadcrumb / tab title.
// ---------------------------------------------------------------------------

function ST_basename(path) {
  if (!path) return "";
  var parts = String(path).split("/");
  return parts[parts.length - 1] || path;
}

// ---------------------------------------------------------------------------
// ST_fileClassOf — coarse render-class for a path: "markdown"|"image"|"code"|"text".
// Drives the preview branch + whether syntax highlighting applies.
// ---------------------------------------------------------------------------

var ST_IMAGE_EXTS = { png: 1, jpg: 1, jpeg: 1, gif: 1, svg: 1, webp: 1, bmp: 1, ico: 1, avif: 1 };
var ST_MD_EXTS = { md: 1, markdown: 1, mdx: 1 };
var ST_CODE_LANGS = {
  py: "python", pyi: "python",
  js: "javascript", jsx: "javascript", ts: "javascript", tsx: "javascript",
  json: "json",
  sh: "bash", bash: "bash", zsh: "bash",
  yaml: "yaml", yml: "yaml", toml: "toml", ini: "ini",
  css: "css", html: "html", sql: "sql", rs: "rust", go: "go", c: "c",
};

function ST_extOf(path) {
  var name = ST_basename(path).toLowerCase();
  var dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1) : "";
}

function ST_fileClassOf(path) {
  var ext = ST_extOf(path);
  if (ST_MD_EXTS[ext]) return "markdown";
  if (ST_IMAGE_EXTS[ext]) return "image";
  if (ST_CODE_LANGS[ext]) return "code";
  return "text";
}

function ST_fmtBytes(n) {
  if (typeof n !== "number" || !isFinite(n)) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / (1024 * 1024)).toFixed(1) + " MB";
}

// Files larger than this are preview/download-only (no edit toggle).
var ST_EDIT_MAX_BYTES = 1024 * 1024;

// ---------------------------------------------------------------------------
// CenterTabs — the tab bar over studio.state.openTabs.
//   Each tab: glyph + title + dirty ● (files) + close ×.
//   Active = activeTabId; click → focusTab; × → closeTab (confirm if dirty file).
//   Horizontal overflow scrolls (.st-tabbar { overflow-x:auto }). Empty state
//   shown when there are no open tabs.
// ---------------------------------------------------------------------------

function CenterTabs({ openTabs, activeTabId, onFocus, onClose, onCloseAll }) {
  var tabs = openTabs || [];

  function handleCloseAll(e) {
    e.stopPropagation();
    if (!tabs.length) return; // no-op when there are no open tabs
    // If any file tab has unsaved edits, confirm once before dropping them all.
    var anyDirty = tabs.some(function (t) { return t.kind === "file" && t.dirty; });
    if (!anyDirty) {
      onCloseAll && onCloseAll();
      return;
    }
    confirmDialog({
      title: "Close all tabs?",
      message: "Some files have unsaved changes. Close all tabs without saving?",
      confirmLabel: "Close all",
      danger: true,
    }).then(function (ok) {
      if (ok) onCloseAll && onCloseAll();
    });
  }

  if (tabs.length === 0) {
    return (
      <div className="st-tabbar" data-testid="center-tabs">
        <div
          style={{
            display: "flex",
            alignItems: "center",
            padding: "0 14px",
            fontSize: 11.5,
            color: "var(--text-4)",
            fontStyle: "italic",
          }}
          data-testid="center-tabs-empty"
        >
          No open tabs — pick a session or file from the sidebar.
        </div>
      </div>
    );
  }

  function handleClose(e, tab) {
    e.stopPropagation();
    // A dirty file tab carries unsaved edits — confirm before discarding.
    if (tab.kind === "file" && tab.dirty) {
      confirmDialog({
        title: "Close tab?",
        message: "“" + (tab.title || tab.ref) + "” has unsaved changes. Close without saving?",
        confirmLabel: "Close",
        danger: true,
      }).then(function (ok) {
        if (ok) onClose(tab.id);
      });
      return;
    }
    onClose(tab.id);
  }

  return (
    <div className="st-tabbar" data-testid="center-tabs">
      {tabs.map(function (tab) {
        var isActive = tab.id === activeTabId;
        return (
          <div
            key={tab.id}
            className={"st-tab" + (isActive ? " is-active" : "")}
            data-testid="center-tab"
            data-tab-id={tab.id}
            data-active={isActive ? "true" : "false"}
            title={tab.ref || tab.title}
            onClick={function () { onFocus(tab.id); }}
          >
            {tab.glyph && (
              <span style={{ color: isActive ? "var(--accent)" : "var(--text-4)", flexShrink: 0 }}>
                {tab.glyph}
              </span>
            )}
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {tab.title || ST_basename(tab.ref)}
            </span>
            {tab.kind === "file" && tab.dirty && (
              <span
                data-testid="tab-dirty"
                style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--blue)", flexShrink: 0 }}
              />
            )}
            <span
              className="st-tab-close"
              data-testid="center-tab-close"
              title="Close"
              onClick={function (e) { handleClose(e, tab); }}
            >×</span>
          </div>
        );
      })}
      {/* Close-all control — pinned to the right edge so it stays reachable
          even when the tab strip overflows and scrolls. */}
      <button
        className="st-tabs-close-all"
        data-testid="tabs-close-all"
        title="Close all tabs"
        aria-label="Close all tabs"
        onClick={handleCloseAll}
      >
        <Icon name="x" size={13} />
        <span style={{ fontSize: 11 }}>Close all</span>
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ST_SessionControls — pause/resume · steer · cancel inline control cluster.
// The signal mutations come from the shared window.useSessionControls hook
// (see REUSE note at top). Shared by the agent + graph header rows.
// ---------------------------------------------------------------------------

function ST_SessionControls({ wid, sid, session, pushToast }) {
  var [steerOpen, setSteerOpen] = React.useState(false);
  var [steerText, setSteerText] = React.useState("");

  var status = session && session.status;
  var isTerminal = !!(session && window.SESSION_TERMINAL && window.SESSION_TERMINAL.has(status));

  // Shared pause/resume/steer/cancel mutations (FD1b): extracted to
  // window.useSessionControls so this cluster and any other caller stay in
  // sync. The studio-session:{sid} key keeps the Studio's own session cache
  // fresh; onSteerSuccess clears + closes the steer popover.
  var controls = window.useSessionControls(wid, sid, {
    pushToast: pushToast,
    invalidates: ["studio-session:" + sid],
    onSteerSuccess: function () { setSteerText(""); setSteerOpen(false); },
  });
  var pauseMut = controls.pause;
  var resumeMut = controls.resume;
  var cancelMut = controls.cancel;
  var steerMut = controls.steer;

  function submitSteer() {
    var text = steerText.trim();
    if (!text || !wid) return;
    steerMut.mutate(text);
  }

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, position: "relative" }} data-testid="session-controls">
      <Btn
        size="sm"
        icon="pause"
        disabled={!wid || status !== "running" || pauseMut.loading}
        onClick={function () { if (wid) pauseMut.mutate(); }}
        data-testid="ctrl-pause"
        title={status !== "running" ? "Enabled only when running" : "Pause after current turn"}
      >Pause</Btn>
      <Btn
        size="sm"
        icon="play"
        disabled={!wid || isTerminal || resumeMut.loading}
        onClick={function () { if (wid) resumeMut.mutate(); }}
        data-testid="ctrl-resume"
        title="Resume (idempotent)"
      >Resume</Btn>
      <Btn
        size="sm"
        icon="send"
        disabled={!wid || isTerminal}
        onClick={function () { setSteerOpen(function (o) { return !o; }); }}
        data-testid="ctrl-steer"
        title="Queue a steer instruction"
      >Steer</Btn>
      <Btn
        size="sm"
        kind="danger"
        icon="stop"
        disabled={!wid || isTerminal || cancelMut.loading}
        onClick={function () { if (wid) cancelMut.mutate(); }}
        data-testid="ctrl-cancel"
        title="Cancel the run"
      >Cancel</Btn>

      {steerOpen && (
        <div
          style={{
            position: "absolute",
            top: 30,
            right: 0,
            zIndex: 30,
            width: 320,
            background: "var(--bg-elev)",
            border: "1px solid var(--border-strong)",
            borderRadius: 9,
            boxShadow: "var(--shadow)",
            padding: 10,
          }}
          data-testid="steer-popover"
        >
          <textarea
            placeholder="Drop a hint or directive for the next turn…"
            value={steerText}
            onChange={function (e) { setSteerText(e.target.value); }}
            rows={3}
            autoFocus
            style={{
              width: "100%", padding: "6px 8px", fontSize: 12, background: "var(--bg-2)",
              border: "1px solid var(--border)", borderRadius: 6, color: "var(--text)",
              resize: "none", fontFamily: "IBM Plex Mono, monospace", outline: "none", marginBottom: 8,
            }}
          />
          <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
            <Btn size="sm" kind="ghost" onClick={function () { setSteerOpen(false); }}>Cancel</Btn>
            <Btn size="sm" kind="primary" icon="send" disabled={!steerText.trim() || steerMut.loading} onClick={submitSteer}>Queue</Btn>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ST_TokenMeterInline — compact token meter for the agent header. Reuses the
// shared window.TokenMeter when present; degrades to a plain count otherwise.
// ---------------------------------------------------------------------------

function ST_TokenMeterInline({ session }) {
  var turns = (session && Array.isArray(session.turns)) ? session.turns : [];
  var last = turns.length > 0 ? turns[turns.length - 1] : null;
  var inputTokens = Number(last && last.tokens_in) || 0;
  var contextLength = Number(session && session.context_length) || 0;
  if (window.TokenMeter) {
    return <window.TokenMeter inputTokens={inputTokens} contextLength={contextLength} onCompact={null} />;
  }
  return (
    <span className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>{inputTokens} tok</span>
  );
}

// ---------------------------------------------------------------------------
// SessionAgentPanel — agent transcript panel.
//   header: title · status pill · turn · token meter + inline controls
//   body  : the reused SessionLiveStream (session-scoped tap + history)
// ---------------------------------------------------------------------------

function SessionAgentPanel({ wid, sid, session, pushToast }) {
  var StatusPill = window.StatusPill;
  var title = (session && (session.name || session.id)) || sid;
  var turnNo = (session && (session.turn_no != null ? session.turn_no : session.turn_count)) || 0;

  return (
    <div
      data-testid="panel-agent"
      style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
    >
      <div
        style={{
          display: "flex", alignItems: "center", gap: 10, padding: "8px 14px",
          borderBottom: "1px solid var(--border)", flex: "0 0 auto", flexWrap: "wrap",
        }}
        data-testid="panel-agent-header"
      >
        <span style={{ color: "var(--accent)", flexShrink: 0 }}>◆</span>
        <span className="mono" style={{ fontSize: 13, fontWeight: 600, color: "var(--text)" }}>{title}</span>
        {StatusPill && session && <StatusPill status={session.status} />}
        <span className="mono" style={{ fontSize: 11, color: "var(--text-4)" }}>turn {turnNo}</span>
        <div style={{ flex: 1 }} />
        <ST_TokenMeterInline session={session} />
        <ST_SessionControls wid={wid} sid={sid} session={session} pushToast={pushToast} />
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        {wid && window.SessionLiveStream
          ? <window.SessionLiveStream sid={sid} wid={wid} session={session} pushToast={pushToast} />
          : (
            <div className="muted text-sm" style={{ padding: 20, textAlign: "center", color: "var(--text-4)" }}>
              Live stream unavailable — session has no workspace.
            </div>
          )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SessionGraphPanel — graph run-view panel.
//   header: name · status · progress (superstep N · X/Y · current)
//   body  : the reused SD_GraphRunView (graph canvas + node inspector)
// ---------------------------------------------------------------------------

function SessionGraphPanel({ wid, sid, session, pushToast }) {
  var StatusPill = window.StatusPill;
  var gid = session && session.binding && session.binding.graph_id
    ? session.binding.graph_id
    : (session && session.graph_id) || null;
  var title = (session && (session.name || session.id)) || sid;
  var superstep = (session && (session.turn_no != null ? session.turn_no : session.turn_count)) || 0;

  // Progress summary if the row carries node counts (best-effort; the
  // run-view itself owns the authoritative per-node state).
  var done = session && (session.nodes_done != null ? session.nodes_done : null);
  var total = session && (session.nodes_total != null ? session.nodes_total : null);
  var current = session && (session.current_node || session.active_node) || null;

  return (
    <div
      data-testid="panel-graph"
      style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
    >
      <div
        style={{
          display: "flex", alignItems: "center", gap: 10, padding: "8px 14px",
          borderBottom: "1px solid var(--border)", flex: "0 0 auto", flexWrap: "wrap",
        }}
        data-testid="panel-graph-header"
      >
        <span style={{ color: "var(--violet)", flexShrink: 0 }}>◈</span>
        <span className="mono" style={{ fontSize: 13, fontWeight: 600, color: "var(--text)" }}>{title}</span>
        {StatusPill && session && <StatusPill status={session.status} />}
        <span className="mono" style={{ fontSize: 11, color: "var(--text-4)" }}>
          superstep {superstep}
          {(done != null && total != null) ? " · " + done + "/" + total : ""}
          {current ? " · " + current : ""}
        </span>
        <div style={{ flex: 1 }} />
        <ST_SessionControls wid={wid} sid={sid} session={session} pushToast={pushToast} />
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        {wid && gid && window.SD_GraphRunView
          ? <window.SD_GraphRunView gid={gid} rid={sid} wid={wid} session={session} pushToast={pushToast} />
          : (
            <div className="muted text-sm" style={{ padding: 20, textAlign: "center", color: "var(--text-4)" }}>
              Run view unavailable — graph binding or workspace missing.
            </div>
          )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ST_SessionPanel — resolves a session tab to the agent or graph panel.
//   Fetches GET /v1/sessions/{ref} (useResource) to read binding.kind.
// ---------------------------------------------------------------------------

function ST_SessionPanel({ wid, sid, pushToast }) {
  var { useResource, apiFetch } = window.primerApi;

  var detail = useResource(
    "studio-session:" + sid,
    function (signal) { return apiFetch("GET", "/sessions/" + encodeURIComponent(sid), null, { signal }); },
    {
      pollMs: 2000,
      pauseWhile: function () {
        // Once terminal, stop polling — nothing left to refresh at this level.
        var st = detail && detail.data && detail.data.status;
        return !!(window.SESSION_TERMINAL && window.SESSION_TERMINAL.has(st));
      },
      deps: [sid],
    }
  );

  var session = detail.data;

  if (detail.loading && !session) {
    return (
      <div className="muted text-sm" style={{ padding: 40, textAlign: "center", color: "var(--text-4)" }}>
        Loading session {sid}…
      </div>
    );
  }
  if (detail.error && !session) {
    return (
      <div style={{ padding: 16 }}>
        <Banner
          kind="error"
          title={(detail.error && detail.error.title) || "Couldn't load session"}
          detail={(detail.error && (detail.error.detail || detail.error.message)) || sid}
          actions={<Btn size="sm" icon="refresh" onClick={detail.refetch}>Retry</Btn>}
        />
      </div>
    );
  }
  if (!session) return null;

  // Resolve binding kind; mirror SessionLiveStream/SD_GraphRunView's defensive
  // reads (binding.kind || binding_kind).
  var kind = (session.binding && session.binding.kind) || session.binding_kind || "agent";
  // The session's own workspace_id is authoritative; fall back to the route wid.
  var effWid = session.workspace_id || wid;

  if (kind === "graph") {
    return <SessionGraphPanel wid={effWid} sid={sid} session={session} pushToast={pushToast} />;
  }
  return <SessionAgentPanel wid={effWid} sid={sid} session={session} pushToast={pushToast} />;
}

// ---------------------------------------------------------------------------
// ST_FilePreview — render the held content per file class.
//   markdown → renderMarkdown; code → highlighted line-numbered <pre>;
//   image → <img> via files/download; else plain text.
// ---------------------------------------------------------------------------

function ST_FilePreview({ wid, path, content }) {
  var cls = ST_fileClassOf(path);

  if (cls === "image") {
    var src = "/v1/workspaces/" + encodeURIComponent(wid) + "/files/download?path=" + encodeURIComponent(path);
    return (
      <div style={{ padding: 18, display: "grid", placeItems: "center" }} data-testid="file-preview-image">
        <img src={src} alt={ST_basename(path)} style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }} />
      </div>
    );
  }

  if (cls === "markdown" && typeof window.renderMarkdown === "function") {
    return (
      <div
        className="md-body"
        data-testid="file-preview-markdown"
        style={{ maxWidth: 760, padding: "18px 22px", fontSize: 13.5, lineHeight: 1.65 }}
      >
        {window.renderMarkdown(content || "")}
      </div>
    );
  }

  if (cls === "code" && window.primerVendor && window.primerVendor.highlightPython) {
    var lang = ST_CODE_LANGS[ST_extOf(path)] || "";
    var htmlLines = window.primerVendor.highlightPython(content || "", lang);
    return (
      <div
        data-testid="file-preview-code"
        style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 12.5, lineHeight: 1.7, padding: "12px 14px" }}
      >
        {htmlLines.map(function (html, i) {
          return (
            <div key={i} style={{ display: "flex" }}>
              <span style={{ color: "var(--text-4)", width: 34, textAlign: "right", marginRight: 14, userSelect: "none", flexShrink: 0 }}>{i + 1}</span>
              <span style={{ whiteSpace: "pre-wrap", flex: 1 }} dangerouslySetInnerHTML={{ __html: html }} />
            </div>
          );
        })}
      </div>
    );
  }

  // Plain text fallback.
  return (
    <pre
      data-testid="file-preview-text"
      style={{
        fontFamily: "IBM Plex Mono, monospace", fontSize: 12.5, lineHeight: 1.7,
        padding: "12px 14px", margin: 0, whiteSpace: "pre-wrap", color: "var(--text-2)",
      }}
    >{content || ""}</pre>
  );
}

// ---------------------------------------------------------------------------
// FilePanel — preview/edit a workspace file with optimistic-concurrency save.
//   header: breadcrumb path + Preview/Edit toggle + Save (enabled when dirty)
//   body  : preview (markdown/code/image/text) OR a <textarea> editor
//   save  : PUT .../files?path=<ref>&etag=<held>; on 412 → conflict banner
//           (Reload re-GETs + clears dirty; Overwrite re-PUTs without etag).
//   binary/large(>~1MB): preview/download only (no edit toggle).
// ---------------------------------------------------------------------------

function FilePanel({ wid, tab, studio, pushToast }) {
  var { useResource, apiFetch } = window.primerApi;
  var path = tab.ref;
  var tabId = tab.id;
  var mode = (studio.state.fileModes && studio.state.fileModes[tabId]) || "preview";

  // The held read: content + etag we opened the file at. The etag we PUT with
  // is held in a ref so editing doesn't refetch and a successful save can
  // refresh it without re-reading the file.
  var fileRes = useResource(
    "studio-file:" + wid + ":" + path,
    function (signal) {
      return apiFetch(
        "GET",
        "/workspaces/" + encodeURIComponent(wid) + "/files/read?path=" + encodeURIComponent(path) + "&encoding=text",
        null,
        { signal }
      );
    },
    { pollMs: 0, deps: [wid, path] }
  );

  var heldEtagRef = React.useRef(null);
  var [draft, setDraft] = React.useState(null);
  var [conflict, setConflict] = React.useState(false);
  var [saving, setSaving] = React.useState(false);

  var data = fileRes.data || null;
  var content = data && typeof data.content === "string" ? data.content : "";
  var sizeBytes = data && typeof data.size_bytes === "number" ? data.size_bytes : (content ? content.length : 0);
  var isImage = ST_fileClassOf(path) === "image";
  var encoding = data && data.encoding;
  var isBinary = encoding && encoding !== "text";
  var tooLarge = sizeBytes > ST_EDIT_MAX_BYTES;
  var editable = !isImage && !isBinary && !tooLarge;

  // Seed the editor draft + held etag whenever a fresh read lands. Editing
  // mutates `draft` locally; the held etag gates the next save.
  React.useEffect(function () {
    if (data && data.etag !== undefined) {
      heldEtagRef.current = data.etag;
    }
    if (data && typeof data.content === "string") {
      setDraft(data.content);
    }
  }, [data]);

  // A dirty tab is one whose draft diverges from the held content. We mirror
  // that into studio.state.openTabs[*].dirty via the patch() escape hatch so
  // the tab bar + sidebar dirty dots reflect it.
  function setTabDirty(dirty) {
    var openTabs = (studio.state.openTabs || []).map(function (t) {
      if (t.id !== tabId) return t;
      if (!!t.dirty === !!dirty) return t;
      return Object.assign({}, t, { dirty: !!dirty });
    });
    // Only patch when something actually changed (avoid render churn).
    var changed = false;
    for (var i = 0; i < openTabs.length; i++) {
      if (openTabs[i] !== (studio.state.openTabs || [])[i]) { changed = true; break; }
    }
    if (changed) studio.patch({ openTabs: openTabs });
  }

  function onEditInput(e) {
    var next = e.target.value;
    setDraft(next);
    setTabDirty(next !== content);
  }

  function setMode(m) {
    // B1's useStudioState exposes only patch(); the per-tab file mode lives in
    // state.fileModes keyed by tab id.
    var nextModes = Object.assign({}, studio.state.fileModes);
    nextModes[tabId] = m;
    studio.patch({ fileModes: nextModes });
  }

  function toastErr(title, err) {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: (err && err.title) || title,
      detail: (err && (err.detail || err.message)),
      requestId: err && err.requestId,
    });
  }

  // PUT the draft. When `withEtag` is true we send the held etag so the server
  // 412s on a stale write (someone else wrote the file since we opened it);
  // when false we force-overwrite (the Overwrite conflict action).
  async function doSave(withEtag) {
    if (draft == null) return;
    setSaving(true);
    var url = "/workspaces/" + encodeURIComponent(wid) + "/files?path=" + encodeURIComponent(path);
    if (withEtag && heldEtagRef.current) {
      url += "&etag=" + encodeURIComponent(heldEtagRef.current);
    }
    try {
      await apiFetch("PUT", url, { content: draft, encoding: "text" });
      // Success: clear dirty + conflict, refresh the held etag from a re-read.
      setConflict(false);
      setTabDirty(false);
      setSaving(false);
      pushToast && pushToast({ kind: "success", title: "File saved", detail: path });
      // Trigger a re-read to refresh the held etag. refetch() returns undefined
      // here (useResource), so adoption happens in the [data] effect above,
      // which re-seeds heldEtagRef + draft once the new read commits.
      await fileRes.refetch();
    } catch (err) {
      setSaving(false);
      if (err && err.status === 412) {
        // Stale write — surface the conflict banner (Reload / Overwrite).
        setConflict(true);
        return;
      }
      toastErr("Save failed", err);
    }
  }

  function onSave() { doSave(true); }

  // Reload: discard local edits, re-GET (replaces content), clear dirty.
  async function reloadConflict() {
    setConflict(false);
    setTabDirty(false);
    // Trigger a re-read. refetch() returns undefined (useResource), so the
    // [data] effect above performs the adoption: it replaces `draft` with the
    // fresh content and re-seeds heldEtagRef once the new read commits.
    await fileRes.refetch();
  }

  // Overwrite: re-PUT WITHOUT the etag (force), keeping local edits.
  function overwriteConflict() {
    setConflict(false);
    doSave(false);
  }

  var dirty = (draft != null) && (draft !== content);
  var effMode = editable ? mode : "preview";

  // ----- header -----
  var header = (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 8, padding: "8px 14px",
        borderBottom: "1px solid var(--border)", flex: "0 0 auto",
        fontFamily: "IBM Plex Mono, monospace", fontSize: 12, color: "var(--text-3)",
      }}
      data-testid="panel-file-header"
    >
      <span style={{ color: "var(--text-2)", display: "grid", placeItems: "center", flexShrink: 0 }}>
        <Icon name={isImage ? "image" : ST_fileClassOf(path) === "code" ? "code" : "doc"} size={13} />
      </span>
      <span data-testid="file-breadcrumb" style={{ color: "var(--text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{path}</span>
      {sizeBytes ? <span style={{ flexShrink: 0 }}>· {ST_fmtBytes(sizeBytes)}</span> : null}
      <div style={{ flex: 1 }} />

      {editable && (
        <div style={{ display: "flex", border: "1px solid var(--border)", borderRadius: 7, overflow: "hidden", flexShrink: 0 }}>
          <button
            data-testid="file-mode-preview"
            onClick={function () { setMode("preview"); }}
            style={{
              padding: "3px 9px", fontSize: 11.5, border: "none", cursor: "pointer",
              background: effMode === "preview" ? "var(--accent-dim)" : "var(--bg-2)",
              color: effMode === "preview" ? "var(--accent)" : "var(--text-3)",
            }}
          >◉ Preview</button>
          <button
            data-testid="file-mode-edit"
            onClick={function () { setMode("edit"); }}
            style={{
              padding: "3px 9px", fontSize: 11.5, border: "none", cursor: "pointer",
              borderLeft: "1px solid var(--border)",
              background: effMode === "edit" ? "var(--accent-dim)" : "var(--bg-2)",
              color: effMode === "edit" ? "var(--accent)" : "var(--text-3)",
            }}
          >✎ Edit</button>
        </div>
      )}

      {editable && (
        <button
          data-testid="file-save"
          onClick={onSave}
          disabled={!dirty || saving}
          title={dirty ? "Save (PUT with etag)" : "No unsaved changes"}
          style={{
            padding: "3px 10px", fontSize: 11.5, fontWeight: 600, borderRadius: 6,
            border: "1px solid oklch(0.82 0.18 145 / 0.4)", cursor: dirty && !saving ? "pointer" : "default",
            background: "var(--green-dim)", color: "var(--green)",
            opacity: dirty && !saving ? 1 : 0.45, flexShrink: 0,
          }}
        >⤓ {saving ? "Saving…" : "Save"}</button>
      )}

      {!editable && (
        <a
          data-testid="file-download"
          href={"/v1/workspaces/" + encodeURIComponent(wid) + "/files/download?path=" + encodeURIComponent(path)}
          target="_blank"
          rel="noreferrer noopener"
          style={{ padding: "3px 10px", fontSize: 11.5, color: "var(--text-2)", textDecoration: "none", border: "1px solid var(--border)", borderRadius: 6, flexShrink: 0 }}
        >↓ Download</a>
      )}
    </div>
  );

  // ----- body -----
  var body;
  if (fileRes.loading && !data) {
    body = <div className="muted text-sm" style={{ padding: 24, textAlign: "center", color: "var(--text-4)" }}>Loading {path}…</div>;
  } else if (fileRes.error && !data) {
    body = (
      <div style={{ padding: 16 }}>
        <Banner
          kind="error"
          title={(fileRes.error && fileRes.error.title) || "Couldn't read file"}
          detail={(fileRes.error && (fileRes.error.detail || fileRes.error.message)) || path}
          actions={<Btn size="sm" icon="refresh" onClick={fileRes.refetch}>Retry</Btn>}
        />
      </div>
    );
  } else if (effMode === "edit") {
    body = (
      <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
        {conflict && (
          <div
            data-testid="file-conflict-banner"
            style={{
              margin: "12px 14px 0", padding: "8px 12px", borderRadius: 7,
              border: "1px solid oklch(0.82 0.16 75 / 0.4)", background: "var(--amber-dim)",
              color: "var(--amber)", fontSize: 12, display: "flex", alignItems: "center", gap: 10,
            }}
          >
            <span style={{ flex: 1 }}>⚠ This file changed on disk since you opened it.</span>
            <b data-testid="file-conflict-reload" onClick={reloadConflict} style={{ cursor: "pointer", textDecoration: "underline" }}>Reload</b>
            <b data-testid="file-conflict-overwrite" onClick={overwriteConflict} style={{ cursor: "pointer", textDecoration: "underline" }}>Overwrite</b>
          </div>
        )}
        <textarea
          data-testid="file-editor"
          value={draft == null ? "" : draft}
          onChange={onEditInput}
          spellCheck={false}
          style={{
            flex: 1, minHeight: 320, width: "100%", background: "var(--bg)", border: 0,
            color: "var(--text)", fontFamily: "IBM Plex Mono, monospace", fontSize: 12.5,
            lineHeight: 1.7, padding: "14px 16px", resize: "none", tabSize: 2, outline: "none",
          }}
        />
      </div>
    );
  } else {
    body = <ST_FilePreview wid={wid} path={path} content={content} />;
  }

  return (
    <div
      data-testid="panel-file"
      style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
    >
      {header}
      <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        {body}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// StudioCenter — center region: CenterTabs + the active panel.
// Props: { wid, studio }  (studio = useStudioState(wid) bag)
// ---------------------------------------------------------------------------

function StudioCenter({ wid, studio }) {
  var s = studio.state;
  var openTabs = s.openTabs || [];
  var activeTabId = s.activeTabId;
  // pushToast threads down from app.jsx → Studio when wired; B1 renders Studio
  // without it today, so every toast call below is guarded and simply no-ops
  // until a later task passes it. The 412-conflict UX uses an inline banner
  // (not a toast), so the critical flow does not depend on this.
  var pushToast = studio.pushToast || (window.primerApi && window.primerApi.toastPush) || null;

  var activeTab = null;
  for (var i = 0; i < openTabs.length; i++) {
    if (openTabs[i].id === activeTabId) { activeTab = openTabs[i]; break; }
  }

  var panel;
  if (!activeTab) {
    panel = (
      <div
        className="st-placeholder"
        data-testid="center-empty"
        style={{ flex: 1 }}
      >
        <div>
          <div className="st-ph-kind">Studio</div>
          <div style={{ marginTop: 6 }}>Open a session or file from the sidebar to begin.</div>
        </div>
      </div>
    );
  } else if (activeTab.kind === "session") {
    // key by ref so switching session tabs remounts the resolver cleanly.
    panel = <ST_SessionPanel key={"sess:" + activeTab.ref} wid={wid} sid={activeTab.ref} pushToast={pushToast} />;
  } else if (activeTab.kind === "file") {
    panel = <FilePanel key={"file:" + activeTab.ref} wid={wid} tab={activeTab} studio={studio} pushToast={pushToast} />;
  } else {
    panel = (
      <div className="st-placeholder" data-testid="center-unknown" style={{ flex: 1 }}>
        <div>Unknown tab kind: {String(activeTab.kind)}</div>
      </div>
    );
  }

  return (
    <div
      data-testid="studio-center-inner"
      style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, overflow: "hidden" }}
    >
      <CenterTabs
        openTabs={openTabs}
        activeTabId={activeTabId}
        onFocus={studio.focusTab}
        onClose={studio.closeTab}
        onCloseAll={studio.closeAllTabs}
      />
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        {panel}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// No-build exports
// ---------------------------------------------------------------------------
window.StudioCenter = StudioCenter;
window.CenterTabs = CenterTabs;
window.SessionAgentPanel = SessionAgentPanel;
window.SessionGraphPanel = SessionGraphPanel;
window.FilePanel = FilePanel;
