/* global React, Icon, StatusPill, Btn, Modal, Banner, relativeTime, fmtDate */

// Workspaces page + detail wired to the real API. The Designer's mock-data
// scaffold was replaced in Phase 2 — every fetch goes through
// window.primerApi.{apiFetch, useResource, useMutation}. Cache-key convention
// follows other components: "workspaces:list", "workspace-detail:${wid}",
// "workspace-files:${wid}:${path}", "workspace-sessions:${wid}",
// "workspace-log:${wid}:${limit}", "workspace-channels:${wid}".
//
// Babel-standalone shares the global scope across <script> tags so every
// top-level binding in this file is prefixed with WS_ to avoid name clashes
// with channels.jsx (ProviderBadge, Toggle) and others.

const WS_TERMINAL = new Set(["ended", "completed", "failed", "cancelled"]);

function WS_PhasePill({ phase }) {
  // Follows the inline-styled pill convention used in toolsets.jsx and
  // channels.jsx — pairs the .pill / .dot classes from app.css with a phase-
  // specific tint so the colour palette stays in sync with the rest of the UI.
  const color =
    phase === "running" ? "var(--green)" :
    phase === "pending" ? "var(--amber)" :
    phase === "failed" ? "var(--red)" :
    phase === "terminating" ? "var(--text-3)" : "var(--text-3)";
  return (
    <span className="pill" style={{ color, borderColor: "var(--border)", background: "var(--bg-2)" }}>
      <span className="dot" style={{ background: color }}></span>
      {phase || "unknown"}
    </span>
  );
}

function _wsAgeSec(iso) {
  if (!iso) return null;
  if (iso instanceof Date) return (Date.now() - iso.getTime()) / 1000;
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

function _wsToastErr(pushToast, fallbackTitle) {
  return (err) => {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: err?.title || fallbackTitle,
      detail: err?.detail || err?.message,
      requestId: err?.requestId,
    });
  };
}

// ============================================================================
// Workspaces list page
// ============================================================================

function WorkspacesPage({ onOpen, pushToast }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  const [createOpen, setCreateOpen] = React.useState(false);
  const [textQuery, setTextQuery] = React.useState("");
  const [templateFilter, setTemplateFilter] = React.useState("");
  const [providerFilter, setProviderFilter] = React.useState("");
  const filterFocused = React.useRef(false);

  const list = useResource(
    "workspaces:list",
    (signal) => apiFetch("GET", "/workspaces?limit=200", null, { signal }),
    { pollMs: 5000, pauseWhile: () => filterFocused.current }
  );

  const items = list.data?.items ?? [];

  const filtered = React.useMemo(() => {
    let arr = items;
    if (textQuery) {
      const q = textQuery.toLowerCase();
      arr = arr.filter((w) =>
        (w.id || "").toLowerCase().includes(q) ||
        (w.template_id || "").toLowerCase().includes(q) ||
        (w.provider_id || "").toLowerCase().includes(q)
      );
    }
    if (templateFilter) arr = arr.filter((w) => w.template_id === templateFilter);
    if (providerFilter) arr = arr.filter((w) => w.provider_id === providerFilter);
    return arr;
  }, [items, textQuery, templateFilter, providerFilter]);

  const templates = React.useMemo(() => {
    const set = new Set();
    for (const w of items) if (w.template_id) set.add(w.template_id);
    return [...set].sort();
  }, [items]);
  const providers = React.useMemo(() => {
    const set = new Set();
    for (const w of items) if (w.provider_id) set.add(w.provider_id);
    return [...set].sort();
  }, [items]);

  const openRow = (wid) => {
    if (typeof onOpen === "function") onOpen(wid);
    else navigate("/workspaces/" + wid);
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter workspaces…"
            value={textQuery}
            onChange={(e) => setTextQuery(e.target.value)}
            onFocus={() => { filterFocused.current = true; }}
            onBlur={() => { filterFocused.current = false; }}
          />
        </div>
        <div className="sep-v" />
        <select
          className="ws-filter-select"
          value={templateFilter}
          onChange={(e) => setTemplateFilter(e.target.value)}
          data-testid="workspaces-template-filter"
        >
          <option value="">all templates</option>
          {templates.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <select
          className="ws-filter-select"
          value={providerFilter}
          onChange={(e) => setProviderFilter(e.target.value)}
          data-testid="workspaces-provider-filter"
        >
          <option value="">all providers</option>
          {providers.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New workspace</Btn>
        </div>
      </div>

      {list.error && items.length === 0 ? (
        <Banner
          kind="error"
          title={list.error.title || "Couldn't load workspaces"}
          detail={list.error.detail || list.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
        />
      ) : items.length === 0 && !list.loading ? (
        <div className="panel">
          <div className="empty">
            <div className="ico-wrap"><Icon name="box" size={22} /></div>
            <div className="head">No workspaces yet</div>
            <div className="sub">A workspace is a materialised template — the per-session filesystem + state repo that agents read and write. Create one to get started.</div>
            <div className="actions">
              <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New workspace</Btn>
            </div>
          </div>
        </div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>ID</th>
                <th>Template</th>
                <th>Provider</th>
                <th>Phase</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                  No workspaces match the current filter{textQuery ? ` "${textQuery}"` : ""}.
                  {" · "}<a
                    onClick={() => { setTextQuery(""); setTemplateFilter(""); setProviderFilter(""); }}
                    style={{ cursor: "pointer", color: "var(--accent)" }}
                  >Clear filters</a>
                </td></tr>
              ) : filtered.map((w) => (
                <tr key={w.id} onClick={() => openRow(w.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{w.id}</td>
                  <td className="mono">{w.template_id}</td>
                  <td className="mono muted">{w.provider_id}</td>
                  <td><WS_PhasePill phase={w.phase} /></td>
                  <td className="mono muted">{w.created_at ? relativeTime(_wsAgeSec(w.created_at)) : "—"}</td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}>
                    <Icon name="chevron-right" size={12} className="muted" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {createOpen && (
        <WS_NewWorkspaceModal
          onClose={() => setCreateOpen(false)}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}

function WS_NewWorkspaceModal({ onClose, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  const templates = useResource(
    "workspaces:templates",
    (signal) => apiFetch("GET", "/workspace_templates?limit=200", null, { signal }),
    {}
  );
  const tplItems = templates.data?.items ?? [];

  const [templateId, setTemplateId] = React.useState("");
  const [templateCreateOpen, setTemplateCreateOpen] = React.useState(false);

  // Auto-pick the first template once it lands so a happy-path submission
  // doesn't need a manual selection if the server only has one template.
  React.useEffect(() => {
    if (!templateId && tplItems.length > 0) {
      setTemplateId(tplItems[0].id);
    }
  }, [tplItems, templateId]);

  const create = useMutation(
    (body) => apiFetch("POST", "/workspaces", body),
    {
      invalidates: ["workspaces:list"],
      onSuccess: (row) => {
        onClose();
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Workspace created", detail: row.id });
        }
        navigate("/workspaces/" + row.id);
      },
      onError: _wsToastErr(pushToast, "Create failed"),
    }
  );

  const onCreate = () => {
    if (!templateId) return;
    create.mutate({ template_id: templateId });
  };

  return (
    <Modal
      title="New workspace"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn
            kind="primary"
            icon="plus"
            disabled={!templateId || create.loading}
            onClick={onCreate}
          >Create</Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">Template</label>
        {templates.loading && tplItems.length === 0 ? (
          <div className="muted text-sm">Loading templates…</div>
        ) : tplItems.length === 0 ? (
          <div className="col" style={{ gap: 10 }}>
            <div className="banner banner-warning" style={{ margin: 0, fontSize: 11.5 }}>
              <Icon name="alert" size={12} className="ico" />
              <div>No workspace_templates registered. Create one to continue.</div>
            </div>
            <Btn size="sm" kind="primary" icon="plus" onClick={() => setTemplateCreateOpen(true)}>Create a template now</Btn>
          </div>
        ) : (
          <select
            className="select mono"
            style={{ width: "100%" }}
            value={templateId}
            onChange={(e) => setTemplateId(e.target.value)}
          >
            {tplItems.map((t) => (
              <option key={t.id} value={t.id}>{t.id}</option>
            ))}
          </select>
        )}
      </div>
      <div className="banner banner-info" style={{ margin: 0, fontSize: 11.5 }}>
        <Icon name="info" size={12} className="ico" />
        <div>ID is generated by the backend — any <span className="mono">id</span> field in this body is silently ignored (spec §12).</div>
      </div>
      {templateCreateOpen && (
        <window.WorkspaceTemplateCreateModal
          onClose={() => {
            setTemplateCreateOpen(false);
            templates.refetch();
          }}
          pushToast={pushToast}
        />
      )}
    </Modal>
  );
}

// ============================================================================
// Detail
// ============================================================================

const WS_TABS = ["files", "sessions", "log", "channels", "config", "destroy"];

function WorkspaceDetail({ workspaceId, onOpenSession, onNavigate, pushToast }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { params, query, navigate } = useRouter();
  const wid = workspaceId || params.id;

  const tab = WS_TABS.includes(query.tab) ? query.tab : "files";
  const setTab = (t) => {
    window.location.hash = "#/workspaces/" + encodeURIComponent(wid) + "?tab=" + t;
  };

  const [diagOpen, setDiagOpen] = React.useState(false);

  const ws = useResource(
    `workspace-detail:${wid}`,
    (signal) => apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}`, null, { signal }),
    { deps: [wid] }
  );

  // Only used to flag the Sessions tab badge — same poll cadence as the
  // tab body itself (5s) so the count stays in sync with the rows.
  const sessionsForBadge = useResource(
    `workspace-sessions:${wid}`,
    (signal) => apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/sessions?limit=200`, null, { signal }),
    { pollMs: 5000, deps: [wid] }
  );
  const sessionCount = sessionsForBadge.data?.items?.length ?? null;

  const tabs = [
    { id: "files", label: "Files", icon: "doc" },
    { id: "sessions", label: "Sessions", icon: "zap", count: sessionCount },
    { id: "log", label: "Log", icon: "git-commit" },
    { id: "channels", label: "Channels", icon: "bell" },
    { id: "config", label: "Config", icon: "settings" },
    { id: "destroy", label: "Destroy", icon: "trash", danger: true },
  ];

  const wsRow = ws.data || {};
  const showFailureBanner = wsRow.phase === "failed";
  const failureDetail = (() => {
    const reason = wsRow.failure_reason || "Workspace probe failed";
    if (wsRow.last_probe_at) {
      return `${reason} (last checked: ${wsRow.last_probe_at})`;
    }
    return reason;
  })();

  return (
    <div className="col" style={{ gap: 14 }}>
      {showFailureBanner && (
        <Banner
          kind="error"
          title="Workspace failed"
          detail={failureDetail}
        />
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Btn
          size="sm"
          kind="ghost"
          icon="pause"
          disabled
          title="Workspace pause is reserved (501) — coming soon"
        >Pause</Btn>
        <Btn
          size="sm"
          kind="ghost"
          icon="play"
          disabled
          title="Workspace resume is reserved (501) — coming soon"
        >Resume</Btn>
        <Btn
          size="sm"
          kind="ghost"
          icon="zap"
          onClick={() => setDiagOpen(true)}
        >Run diagnostic</Btn>
      </div>
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              style={{
                background: "none",
                border: "none",
                padding: "10px 14px",
                cursor: "pointer",
                color: tab === t.id ? "var(--text)" : "var(--text-3)",
                fontSize: 12.5,
                fontWeight: tab === t.id ? 600 : 400,
                borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <Icon name={t.icon} size={13} style={{ color: t.danger ? "var(--red)" : undefined }} />
              {t.label}
              {t.count != null && t.count > 0 && (
                <span className="count" style={{ marginLeft: 4 }}>{t.count}</span>
              )}
            </button>
          ))}
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {tab === "files" && <WS_FilesTab wid={wid} pushToast={pushToast} />}
          {tab === "sessions" && <WS_SessionsTab wid={wid} onOpen={onOpenSession} />}
          {tab === "log" && <WS_LogTab wid={wid} />}
          {tab === "channels" && <WS_ChannelsTab wid={wid} pushToast={pushToast} />}
          {tab === "config" && <WS_ConfigTab wid={wid} ws={ws} />}
          {tab === "destroy" && <WS_DestroyTab wid={wid} pushToast={pushToast} sessionsForBadge={sessionsForBadge} />}
        </div>
      </div>
      {diagOpen && (
        <WS_DiagnosticModal workspaceId={wid} onClose={() => setDiagOpen(false)} />
      )}
    </div>
  );
}

// ============================================================================
// Files tab
// ============================================================================

function WS_FilesTab({ wid, pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const [openDirs, setOpenDirs] = React.useState(() => new Set([""]));
  const [selected, setSelected] = React.useState(null);
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState("");

  const toggleDir = (path) => {
    setOpenDirs((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
  };

  const tree = useResource(
    `workspace-files:${wid}:`,
    // API requires a non-empty path; "." is the documented root sentinel
    // (matches primer/api/routers/workspaces.py:375 default).
    (signal) => apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/files?path=.`, null, { signal }),
    { pollMs: 10000, deps: [wid] }
  );

  const fileContent = useResource(
    `workspace-file-content:${wid}:${selected || ""}`,
    (signal) => apiFetch(
      "GET",
      `/workspaces/${encodeURIComponent(wid)}/files/read?path=${encodeURIComponent(selected)}&encoding=text`,
      null,
      { signal }
    ),
    { deps: [wid, selected || ""], pauseWhile: () => !selected }
  );

  // Sync draft to fetched content when not editing.
  React.useEffect(() => {
    if (!editing && fileContent.data && typeof fileContent.data.content === "string") {
      setDraft(fileContent.data.content);
    }
  }, [fileContent.data, editing]);

  const saveFile = useMutation(
    ({ content, encoding }) => apiFetch(
      "PUT",
      `/workspaces/${encodeURIComponent(wid)}/files?path=${encodeURIComponent(selected)}`,
      { content, encoding }
    ),
    {
      invalidates: [
        `workspace-file-content:${wid}:${selected || ""}`,
        `workspace-files:${wid}`,
      ],
      onSuccess: () => {
        setEditing(false);
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "File saved", detail: selected });
        }
      },
      onError: _wsToastErr(pushToast, "Save failed"),
    }
  );

  const selectFile = (path) => {
    setSelected(path);
    setEditing(false);
  };

  const onSave = () => {
    if (!selected) return;
    saveFile.mutate({ content: draft, encoding: "text" });
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "300px 1fr", minHeight: 480, fontSize: 12.5 }}>
      {/* Tree */}
      <div style={{ borderRight: "1px solid var(--border)", overflow: "auto", padding: "10px 0" }}>
        <div style={{ display: "flex", alignItems: "center", padding: "0 12px 8px", gap: 6 }}>
          <span className="mono muted text-sm">/ root</span>
          <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
            <button className="icon-btn" style={{ width: 22, height: 22 }} title="Refresh" onClick={tree.refetch}><Icon name="refresh" size={10} /></button>
          </div>
        </div>
        {tree.error ? (
          <div className="muted text-sm" style={{ padding: "8px 12px" }}>
            {tree.error.title || "Couldn't list files"}
          </div>
        ) : (
          <WS_DirNode
            wid={wid}
            path=""
            depth={0}
            openDirs={openDirs}
            toggleDir={toggleDir}
            selected={selected}
            selectFile={selectFile}
            rootEntries={tree.data?.items ?? []}
          />
        )}
      </div>

      {/* Editor pane */}
      <div style={{ display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", padding: "8px 12px", borderBottom: "1px solid var(--border)", gap: 8 }}>
          <Icon name="doc" size={12} className="muted" />
          <span className="mono" style={{ fontSize: 12 }}>{selected || <span className="muted">no file selected</span>}</span>
          {fileContent.data?.size_bytes != null && (
            <span className="muted mono text-sm">· {fileContent.data.size_bytes} bytes</span>
          )}
          <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            {selected && (
              <>
                {editing ? (
                  <>
                    <Btn size="sm" kind="ghost" onClick={() => { setEditing(false); setDraft(fileContent.data?.content || ""); }}>Discard</Btn>
                    <Btn size="sm" kind="primary" icon="check" disabled={saveFile.loading} onClick={onSave}>Save</Btn>
                  </>
                ) : (
                  <>
                    <a
                      href={`/v1/workspaces/${encodeURIComponent(wid)}/files/download?path=${encodeURIComponent(selected)}`}
                      download
                      style={{ textDecoration: "none" }}
                    >
                      <Btn size="sm" kind="ghost" icon="external">Download</Btn>
                    </a>
                    <Btn size="sm" kind="ghost" icon="copy" onClick={() => setEditing(true)}>Edit</Btn>
                  </>
                )}
              </>
            )}
          </div>
        </div>
        <div style={{ flex: 1, overflow: "auto", background: "var(--bg)" }}>
          {!selected ? (
            <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>
              Click a file in the tree to view its contents.
            </div>
          ) : fileContent.loading && !fileContent.data ? (
            <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>Loading…</div>
          ) : fileContent.error ? (
            <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>
              {fileContent.error.title || "Couldn't read file"}
              {fileContent.error.detail && <div className="text-sm">{fileContent.error.detail}</div>}
            </div>
          ) : editing ? (
            <textarea
              className="textarea mono"
              style={{ width: "100%", border: 0, borderRadius: 0, height: "100%", minHeight: 400, background: "var(--bg)", fontSize: 12, lineHeight: 1.55, padding: 14 }}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />
          ) : (
            <pre className="mono" style={{ margin: 0, padding: 14, fontSize: 12, lineHeight: 1.55, color: "var(--text-2)", whiteSpace: "pre-wrap" }}>
              <WS_CodeHighlight code={fileContent.data?.content || ""} lang={_wsGuessLang(selected)} />
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}

function WS_DirNode({ wid, path, depth, openDirs, toggleDir, selected, selectFile, rootEntries }) {
  // The root node receives its entries pre-fetched (so the tree is one round-
  // trip when closed). Sub-directories lazy-fetch when they're opened.
  const { useResource, apiFetch } = window.primerApi;
  const isRoot = depth === 0;
  const open = openDirs.has(path);

  const sub = useResource(
    `workspace-files:${wid}:${path}`,
    (signal) => apiFetch(
      "GET",
      `/workspaces/${encodeURIComponent(wid)}/files?path=${encodeURIComponent(path || ".")}`,
      null,
      { signal }
    ),
    { deps: [wid, path], pauseWhile: () => isRoot || !open }
  );

  const entries = isRoot ? rootEntries : (sub.data?.items ?? []);
  const sorted = React.useMemo(() => {
    const arr = [...entries];
    arr.sort((a, b) => {
      const ad = a.kind === "dir";
      const bd = b.kind === "dir";
      if (ad && !bd) return -1;
      if (!ad && bd) return 1;
      const aSys = (a.path || "").split("/").pop().startsWith(".");
      const bSys = (b.path || "").split("/").pop().startsWith(".");
      if (aSys && !bSys) return 1;
      if (!aSys && bSys) return -1;
      return (a.path || "").localeCompare(b.path || "");
    });
    return arr;
  }, [entries]);

  return (
    <div>
      {!isRoot && (
        <WS_FileRow
          path={path}
          name={path.split("/").pop()}
          isDir
          open={open}
          depth={depth - 1}
          system={path.startsWith(".state") || path.startsWith(".tmp")}
          onClick={() => toggleDir(path)}
        />
      )}
      {(isRoot || open) && sorted.map((e) => {
        const baseName = (e.path || "").split("/").pop();
        if (e.kind === "dir") {
          return (
            <WS_DirNode
              key={e.path}
              wid={wid}
              path={e.path}
              depth={depth + 1}
              openDirs={openDirs}
              toggleDir={toggleDir}
              selected={selected}
              selectFile={selectFile}
              rootEntries={null}
            />
          );
        }
        return (
          <WS_FileRow
            key={e.path}
            path={e.path}
            name={baseName}
            depth={depth}
            isSelected={e.path === selected}
            onClick={() => selectFile(e.path)}
            size={e.size_bytes}
            system={(e.path || "").startsWith(".state") || (e.path || "").startsWith(".tmp")}
          />
        );
      })}
    </div>
  );
}

function WS_FileRow({ name, depth, isDir, open, isSelected, system, onClick, size }) {
  return (
    <div
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 12px",
        paddingLeft: 12 + Math.max(0, depth) * 14,
        cursor: "pointer",
        background: isSelected ? "var(--accent-dim)" : undefined,
        color: system ? "var(--text-4)" : isSelected ? "var(--text)" : "var(--text-2)",
        fontSize: 12.5,
      }}
      title={system ? "backend-managed; do not edit directly" : undefined}
      onMouseEnter={(e) => { if (!isSelected) e.currentTarget.style.background = "var(--bg-hover)"; }}
      onMouseLeave={(e) => { if (!isSelected) e.currentTarget.style.background = "transparent"; }}
    >
      {isDir ? (
        <>
          <Icon name={open ? "chevron-down" : "chevron-right"} size={10} className="muted" />
          <Icon name="box" size={12} style={{ color: "var(--text-3)" }} />
        </>
      ) : (
        <>
          <span style={{ width: 10 }} />
          <Icon name="doc" size={11} style={{ color: "var(--text-4)" }} />
        </>
      )}
      <span className="mono" style={{ fontSize: 12 }}>{name}</span>
      {!isDir && size != null && (
        <span className="muted mono text-sm" style={{ marginLeft: "auto", fontSize: 10.5 }}>
          {size === 0 ? "0" : size < 1024 ? `${size}B` : `${(size / 1024).toFixed(1)}K`}
        </span>
      )}
    </div>
  );
}

function _wsGuessLang(path) {
  if (!path) return null;
  if (path.endsWith(".py")) return "python";
  if (path.endsWith(".json")) return "json";
  if (path.endsWith(".js") || path.endsWith(".jsx") || path.endsWith(".ts") || path.endsWith(".tsx")) return "js";
  return null;
}

function WS_CodeHighlight({ code, lang }) {
  const lines = (code || "").split("\n");
  const isPy = lang === "python";
  const kw = /\b(import|from|def|class|return|if|else|elif|async|await|yield|in|not|and|or|with|as|for|while|try|except|finally|raise|pass|None|True|False)\b/g;
  return (
    <>
      {lines.map((line, i) => {
        let html = line
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;");
        html = html.replace(/(#.*$)/g, '<span style="color:var(--text-4);font-style:italic">$1</span>');
        html = html.replace(/(""".*?"""|".*?"|'.*?')/g, '<span style="color:var(--green)">$1</span>');
        html = html.replace(/\b(\d+(?:\.\d+)?)\b/g, '<span style="color:var(--amber)">$1</span>');
        if (isPy) {
          html = html.replace(kw, '<span style="color:var(--violet)">$1</span>');
        }
        return (
          <div key={i} style={{ display: "flex" }}>
            <span style={{ width: 36, color: "var(--text-4)", textAlign: "right", paddingRight: 12, userSelect: "none", flexShrink: 0 }}>{i + 1}</span>
            <span dangerouslySetInnerHTML={{ __html: html || "&nbsp;" }} />
          </div>
        );
      })}
    </>
  );
}

// ============================================================================
// Sessions tab — SessionInfo field names per commit 505e76e
// ============================================================================

function WS_SessionsTab({ wid, onOpen }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  const list = useResource(
    `workspace-sessions:${wid}`,
    (signal) => apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/sessions?limit=200`, null, { signal }),
    { pollMs: 5000, deps: [wid] }
  );
  const items = list.data?.items ?? [];

  const openRow = (sid) => {
    if (typeof onOpen === "function") onOpen(sid);
    else navigate("/sessions/" + sid);
  };

  return (
    <div style={{ padding: 14 }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
        <div>
          <div className="text-sm muted">Sessions running on this workspace</div>
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
        </div>
      </div>

      {list.error && items.length === 0 ? (
        <Banner
          kind="error"
          title={list.error.title || "Couldn't load sessions"}
          detail={list.error.detail || list.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
        />
      ) : items.length === 0 ? (
        <div className="empty">
          <div className="ico-wrap"><Icon name="zap" size={18} /></div>
          <div className="head">No sessions</div>
          <div className="sub">Start a session to run an agent (or graph) against this workspace.</div>
        </div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Status</th>
                <th>Session</th>
                <th>Agent</th>
                <th>Started</th>
                <th>Last activity</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => {
                // SessionInfo fields (per primer/model/session.py:188):
                // session_id, agent_id, status, started_at, last_activity_at
                const sid = s.session_id;
                const started = s.started_at ? _wsAgeSec(s.started_at) : null;
                const last = s.last_activity_at ? _wsAgeSec(s.last_activity_at) : null;
                return (
                  <tr key={sid} onClick={() => openRow(sid)} style={{ cursor: "pointer" }}>
                    <td><StatusPill status={s.status} /></td>
                    <td className="mono">{sid}</td>
                    <td className="mono">{s.agent_id || <span className="muted">—</span>}</td>
                    <td className="mono muted">{started != null ? relativeTime(started) : "—"}</td>
                    <td className="mono muted">{last != null ? relativeTime(last) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Log tab — manual refresh, no poll
// ============================================================================

function WS_LogTab({ wid }) {
  const { useResource, apiFetch } = window.primerApi;
  const [limit, setLimit] = React.useState(50);

  const log = useResource(
    `workspace-log:${wid}:${limit}`,
    (signal) => apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/log?limit=${limit}`, null, { signal }),
    { deps: [wid, limit] }
  );

  const commits = log.data?.commits ?? [];

  return (
    <div style={{ padding: 14 }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
        <div className="muted text-sm">
          <span className="mono">git log</span> of the workspace's <span className="mono">.state</span> repository · default limit 50, max 500
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={log.refetch}>Refresh</Btn>
        </div>
      </div>

      {log.error && commits.length === 0 ? (
        <Banner
          kind="error"
          title={log.error.title || "Couldn't load log"}
          detail={log.error.detail || log.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={log.refetch}>Retry</Btn>}
        />
      ) : commits.length === 0 ? (
        <div className="empty">
          <div className="ico-wrap"><Icon name="git-commit" size={18} /></div>
          <div className="head">No commits yet</div>
          <div className="sub">The workspace's <span className="mono">.state</span> repo is empty until a session writes to it.</div>
        </div>
      ) : (
        <>
          <div style={{ position: "relative", paddingLeft: 18 }}>
            <div style={{ position: "absolute", left: 6, top: 6, bottom: 6, width: 1, background: "var(--border)" }}></div>
            {commits.map((e) => {
              const sha = (e.sha || e.id || "").slice(0, 7);
              const at = e.committed_at || e.at;
              const sec = at ? _wsAgeSec(at) : null;
              return (
                <div key={e.sha || e.id} style={{ position: "relative", padding: "6px 0 6px 16px", display: "flex", alignItems: "center", gap: 10 }}>
                  <div style={{ position: "absolute", left: -4, top: 9, width: 11, height: 11, borderRadius: "50%", background: "var(--bg-2)", border: "2px solid var(--accent)" }}></div>
                  <span className="mono" style={{ color: "var(--accent)", fontSize: 12, fontWeight: 500 }}>{sha}</span>
                  {sec != null && <span className="muted mono text-sm">{relativeTime(sec)}</span>}
                  <span style={{ fontSize: 12.5 }}>"{e.message || e.msg || ""}"</span>
                </div>
              );
            })}
          </div>
          <div style={{ marginTop: 14, display: "flex", justifyContent: "center" }}>
            {limit < 500 && (
              <Btn size="sm" kind="ghost" onClick={() => setLimit(Math.min(500, limit + 50))}>Load more</Btn>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ============================================================================
// Channels tab — workspace-scoped channel associations
// ============================================================================

function WS_ChannelsTab({ wid, pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const [showLink, setShowLink] = React.useState(false);

  // GET on the flat endpoint and filter client-side — the scoped GET path is
  // not exposed (only POST scoped). Use the flat list and narrow by wid.
  const all = useResource(
    `workspace-channels:${wid}`,
    (signal) => apiFetch("GET", "/workspace_channel_associations?limit=200", null, { signal }),
    { deps: [wid] }
  );
  const rows = (all.data?.items ?? []).filter((a) => a.workspace_id === wid);

  const channelsList = useResource(
    `workspace-channels-options:${wid}`,
    (signal) => apiFetch("GET", "/channels?limit=200", null, { signal }),
    { deps: [wid] }
  );
  const channels = channelsList.data?.items ?? [];

  return (
    <div style={{ padding: 14 }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
        <div>
          <div className="text-sm muted">Channels associated with this workspace</div>
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowLink(true)}>Link channel</Btn>
        </div>
      </div>

      {all.error && rows.length === 0 ? (
        <Banner
          kind="error"
          title={all.error.title || "Couldn't load channels"}
          detail={all.error.detail || all.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={all.refetch}>Retry</Btn>}
        />
      ) : rows.length === 0 ? (
        <div className="empty">
          <div className="ico-wrap"><Icon name="bell" size={18} /></div>
          <div className="head">No channels linked</div>
          <div className="sub">Link an external channel (Slack, Telegram, Discord) to fan out yielding-tool events from this workspace.</div>
          <div className="actions">
            <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowLink(true)}>Link channel</Btn>
          </div>
        </div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Association</th>
                <th>Channel</th>
                <th>Enabled</th>
                <th>ask_user</th>
                <th>tool_approval</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id}>
                  <td className="mono">{a.id}</td>
                  <td className="mono">{a.channel_id}</td>
                  <td className="mono">{a.enabled ? "yes" : "no"}</td>
                  <td className="mono">{a.forward_ask_user ? "yes" : "no"}</td>
                  <td className="mono">{a.forward_tool_approval ? "yes" : "no"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showLink && (
        <WS_LinkChannelModal
          wid={wid}
          channels={channels}
          onClose={() => setShowLink(false)}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}

function WS_LinkChannelModal({ wid, channels, onClose, pushToast }) {
  const { useMutation, apiFetch } = window.primerApi;
  const [channelId, setChannelId] = React.useState(channels[0]?.id || "");
  const [enabled, setEnabled] = React.useState(true);
  const [forwardAsk, setForwardAsk] = React.useState(true);
  const [forwardApproval, setForwardApproval] = React.useState(true);
  const [assocId, setAssocId] = React.useState("");

  const link = useMutation(
    (body) => apiFetch("POST", `/workspaces/${encodeURIComponent(wid)}/channel_associations`, body),
    {
      invalidates: [`workspace-channels:${wid}`],
      onSuccess: () => {
        onClose();
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Channel linked" });
        }
      },
      onError: _wsToastErr(pushToast, "Link failed"),
    }
  );

  const onLink = () => {
    if (!channelId) return;
    const body = {
      id: assocId || `wca-${wid.slice(0, 6)}-${channelId.slice(0, 6)}-${Math.random().toString(36).slice(2, 8)}`,
      workspace_id: wid,
      channel_id: channelId,
      enabled,
      forward_ask_user: forwardAsk,
      forward_tool_approval: forwardApproval,
    };
    link.mutate(body);
  };

  return (
    <Modal
      title="Link channel"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" disabled={!channelId || link.loading} onClick={onLink}>Link</Btn>
        </>
      }
    >
      <div className="field" style={{ marginBottom: 12 }}>
        <label className="field-label">workspace <span className="hint">auto-filled</span></label>
        <input className="input mono" value={wid} readOnly style={{ width: "100%" }} />
      </div>
      <div className="field">
        <label className="field-label">channel</label>
        {channels.length === 0 ? (
          <div className="muted text-sm">No channels registered. Create one under Channels first.</div>
        ) : (
          <select
            className="select mono"
            style={{ width: "100%" }}
            value={channelId}
            onChange={(e) => setChannelId(e.target.value)}
          >
            {channels.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
          </select>
        )}
      </div>
      <div className="field">
        <label className="field-label">association id <span className="hint">optional</span></label>
        <input
          className="input mono"
          value={assocId}
          onChange={(e) => setAssocId(e.target.value)}
          placeholder="(auto-generated)"
          style={{ width: "100%" }}
        />
      </div>
      <div className="field" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /><span>Enabled</span>
        </label>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
          <input type="checkbox" checked={forwardAsk} onChange={(e) => setForwardAsk(e.target.checked)} /><span>Forward ask_user prompts</span>
        </label>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
          <input type="checkbox" checked={forwardApproval} onChange={(e) => setForwardApproval(e.target.checked)} /><span>Forward tool_approval requests</span>
        </label>
      </div>
    </Modal>
  );
}

// ============================================================================
// Config tab — derived from ws.data (no extra fetch)
// ============================================================================

function WS_ConfigTab({ wid, ws }) {
  if (ws.loading && !ws.data) {
    return <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>Loading workspace…</div>;
  }
  if (ws.error && !ws.data) {
    return (
      <div style={{ padding: 14 }}>
        <Banner
          kind="error"
          title={ws.error.title || "Couldn't load workspace"}
          detail={ws.error.detail || ws.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={ws.refetch}>Retry</Btn>}
        />
      </div>
    );
  }
  const data = ws.data || {};
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        Row materialised from <span className="mono">/v1/workspaces/{wid}</span>.
      </div>
      <dl className="kv" style={{ gridTemplateColumns: "160px 1fr" }}>
        <dt>id</dt><dd className="mono">{data.id || wid}</dd>
        <dt>template_id</dt><dd className="mono">{data.template_id || "—"}</dd>
        <dt>provider_id</dt><dd className="mono">{data.provider_id || "—"}</dd>
        <dt>created_at</dt><dd className="mono">{data.created_at ? fmtDate(new Date(data.created_at)) : "—"}</dd>
      </dl>
      {data.overrides && (
        <div className="mt-4">
          <div className="field-label" style={{ marginBottom: 6 }}>Overrides</div>
          <div className="code-block" style={{ maxHeight: 360, overflow: "auto" }}>
            {JSON.stringify(data.overrides, null, 2)}
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Destroy tab — confirmation modal then DELETE
// ============================================================================

function WS_DestroyTab({ wid, pushToast, sessionsForBadge }) {
  const { useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const [showConfirm, setShowConfirm] = React.useState(false);
  const [cascadeError, setCascadeError] = React.useState(null);

  const items = sessionsForBadge?.data?.items ?? [];
  const active = items.filter((s) => !WS_TERMINAL.has(s.status));

  const destroy = useMutation(
    () => apiFetch("DELETE", `/workspaces/${encodeURIComponent(wid)}`),
    {
      invalidates: ["workspaces:list"],
      onSuccess: () => {
        if (typeof pushToast === "function") {
          pushToast({ kind: "warning", title: "Workspace destroyed", detail: wid });
        }
        navigate("/workspaces");
      },
      onError: (err) => {
        if (err && err.status === 409) {
          setCascadeError(err);
          return;
        }
        if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err?.title || "Destroy failed",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    }
  );

  const onConfirm = () => {
    setShowConfirm(false);
    setCascadeError(null);
    destroy.mutate();
  };

  return (
    <div style={{ padding: 14 }}>
      <Banner
        kind="error"
        title="Permanent action"
        detail="Destroying a workspace cancels any in-flight sessions, deletes the .state repo, and removes all files. This cannot be undone."
      />
      {cascadeError && (
        <div className="mt-3">
          <Banner
            kind="error"
            title={cascadeError.title || "Destroy blocked"}
            detail={cascadeError.detail || cascadeError.message || "Cascade conflict — resolve dependent rows first."}
          />
        </div>
      )}
      <div className="kv mt-3" style={{ gridTemplateColumns: "160px 1fr" }}>
        <dt>workspace</dt><dd className="mono">{wid}</dd>
        <dt>active sessions</dt><dd className="mono">{active.length} {active.length > 0 && <span className="muted">(will be cancelled)</span>}</dd>
        <dt>total sessions</dt><dd className="mono">{items.length}</dd>
      </div>
      <div className="mt-4">
        <Btn kind="danger" icon="trash" disabled={destroy.loading} onClick={() => setShowConfirm(true)}>Destroy workspace</Btn>
      </div>

      {showConfirm && (
        <Modal
          title={`Destroy ${wid}?`}
          danger
          onClose={() => setShowConfirm(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setShowConfirm(false)}>Cancel</Btn>
              <Btn kind="danger" icon="trash" disabled={destroy.loading} onClick={onConfirm}>Destroy permanently</Btn>
            </>
          }
        >
          <ul>
            <li><strong>{active.length}</strong> active session(s) on this workspace will be <strong>cancelled before destroy</strong>.</li>
            <li>The <span className="mono">.state</span> git repo will be deleted <strong>permanently</strong> — turn history will be gone.</li>
            <li>Any files written by sessions are removed.</li>
            <li>This cannot be undone.</li>
          </ul>
        </Modal>
      )}
    </div>
  );
}

// ============================================================================
// Diagnostic modal — POSTs to /v1/workspaces/{id}/diagnostic with a
// whitelisted command (echo, pwd, whoami, uname, ls) and renders
// stdout/stderr/exit_code. Surfaces the backend whitelist as a <select>
// so users can't trip the 400 by typing a non-whitelisted head token.
// ============================================================================

function WS_DiagnosticModal({ workspaceId, onClose }) {
  const { apiFetch } = window.primerApi;
  const [command, setCommand] = React.useState("echo");
  const [args, setArgs] = React.useState("hello");
  const [result, setResult] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  const run = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const trimmedArgs = (args || "").trim();
      const fullCommand = trimmedArgs ? `${command} ${trimmedArgs}` : command;
      const data = await apiFetch(
        "POST",
        `/workspaces/${encodeURIComponent(workspaceId)}/diagnostic`,
        { command: fullCommand }
      );
      setResult(data);
    } catch (e) {
      // apiFetch throws an ApiError with .title/.detail/.message — fall back
      // to a plain string for native errors (network, etc.).
      const detail = e?.detail || e?.message || String(e);
      setError(typeof detail === "string" ? detail : JSON.stringify(detail));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="Run diagnostic command"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Close</Btn>
          <Btn kind="primary" icon="zap" disabled={busy} onClick={run}>Run</Btn>
        </>
      }
    >
      <div className="field" style={{ marginBottom: 12 }}>
        <label className="field-label">workspace <span className="hint">auto-filled</span></label>
        <input className="input mono" value={workspaceId} readOnly style={{ width: "100%" }} />
      </div>
      <div className="field" style={{ marginBottom: 12 }}>
        <label className="field-label">command <span className="hint">whitelisted</span></label>
        <select
          className="select mono"
          style={{ width: "100%" }}
          value={command}
          onChange={(e) => setCommand(e.target.value)}
        >
          <option value="echo">echo</option>
          <option value="pwd">pwd</option>
          <option value="whoami">whoami</option>
          <option value="uname">uname</option>
          <option value="ls">ls</option>
        </select>
      </div>
      <div className="field" style={{ marginBottom: 12 }}>
        <label className="field-label">arguments <span className="hint">optional</span></label>
        <input
          className="input mono"
          value={args}
          onChange={(e) => setArgs(e.target.value)}
          placeholder="(optional)"
          style={{ width: "100%" }}
        />
      </div>
      {error && (
        <div style={{ marginTop: 8 }}>
          <Banner kind="error" title="Diagnostic failed" detail={error} />
        </div>
      )}
      {result && (
        <div style={{ marginTop: 8 }}>
          <div className="muted text-sm" style={{ marginBottom: 6 }}>
            exit_code: <span className="mono">{result.exit_code}</span>
            {typeof result.duration_seconds === "number" && (
              <> · duration: <span className="mono">{result.duration_seconds.toFixed(3)}s</span></>
            )}
          </div>
          {result.stdout && (
            <pre className="mono" style={{ maxHeight: 240, overflow: "auto", background: "var(--bg-2)", padding: 8, borderRadius: 4, fontSize: 12 }}>
              stdout:{"\n"}{result.stdout}
            </pre>
          )}
          {result.stderr && (
            <pre className="mono" style={{ maxHeight: 240, overflow: "auto", background: "var(--bg-2)", padding: 8, borderRadius: 4, fontSize: 12 }}>
              stderr:{"\n"}{result.stderr}
            </pre>
          )}
          {!result.stdout && !result.stderr && (
            <div className="muted text-sm">(no output)</div>
          )}
        </div>
      )}
    </Modal>
  );
}

window.WorkspacesPage = WorkspacesPage;
window.WorkspaceDetail = WorkspaceDetail;
