/* global React, Icon, StatusPill, Btn, Modal, Banner, CardList, Card, Fab, MobileTabs, relativeTime, fmtDate */

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
  const { useResource, useRouter, useViewport, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const { isMobile } = useViewport();

  const [createOpen, setCreateOpen] = React.useState(false);
  const [renaming, setRenaming] = React.useState(null);
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
        (w.name || "").toLowerCase().includes(q) ||
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
      ) : isMobile ? (
        <CardList
          items={filtered}
          empty={
            <span>
              No workspaces match the current filter{textQuery ? ` "${textQuery}"` : ""}.
              {" · "}<a
                onClick={() => { setTextQuery(""); setTemplateFilter(""); setProviderFilter(""); }}
                style={{ cursor: "pointer", color: "var(--accent)" }}
              >Clear filters</a>
            </span>
          }
          renderCard={(w) => (
            <Card
              title={w.name || w.id}
              subtitle={`${w.name ? w.id + " · " : ""}${w.template_id || "—"} · ${w.provider_id || "—"}`}
              pill={<WS_PhasePill phase={w.phase} />}
              meta={w.created_at ? relativeTime(_wsAgeSec(w.created_at)) : "—"}
              onClick={() => openRow(w.id)}
            />
          )}
        />
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
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
                <tr><td colSpan={7} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                  No workspaces match the current filter{textQuery ? ` "${textQuery}"` : ""}.
                  {" · "}<a
                    onClick={() => { setTextQuery(""); setTemplateFilter(""); setProviderFilter(""); }}
                    style={{ cursor: "pointer", color: "var(--accent)" }}
                  >Clear filters</a>
                </td></tr>
              ) : filtered.map((w) => (
                <tr key={w.id} onClick={() => openRow(w.id)} style={{ cursor: "pointer" }}>
                  <td>
                    {w.name
                      ? <span>{w.name}</span>
                      : <span className="muted text-sm" style={{ fontStyle: "italic" }}>unnamed</span>}
                  </td>
                  <td className="mono muted text-sm">{w.id}</td>
                  <td className="mono">{w.template_id}</td>
                  <td className="mono muted">{w.provider_id}</td>
                  <td><WS_PhasePill phase={w.phase} /></td>
                  <td className="mono muted">{w.created_at ? relativeTime(_wsAgeSec(w.created_at)) : "—"}</td>
                  <td style={{ textAlign: "right", paddingRight: 12, whiteSpace: "nowrap" }}>
                    <Btn
                      size="sm"
                      kind="ghost"
                      icon="edit"
                      title="Rename workspace"
                      onClick={(e) => { e.stopPropagation(); setRenaming(w); }}
                    />
                    <Icon name="chevron-right" size={12} className="muted" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {isMobile && (
        <Fab icon="plus" label="New workspace" onClick={() => setCreateOpen(true)} />
      )}

      {createOpen && (
        <WS_NewWorkspaceModal
          onClose={() => setCreateOpen(false)}
          pushToast={pushToast}
        />
      )}
      {renaming && (
        <WS_RenameWorkspaceModal
          workspace={renaming}
          onClose={() => setRenaming(null)}
          onRenamed={() => { setRenaming(null); list.refetch(); }}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}

function WS_RenameWorkspaceModal({ workspace, onClose, onRenamed, pushToast }) {
  const { useMutation, apiFetch } = window.primerApi;
  const [name, setName] = React.useState(workspace.name || "");

  const rename = useMutation(
    (body) => apiFetch("PATCH", "/workspaces/" + encodeURIComponent(workspace.id), body),
    {
      invalidates: ["workspaces:list", "workspace-detail:" + workspace.id],
      onSuccess: () => {
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Workspace renamed", detail: workspace.id });
        }
        onRenamed();
      },
      onError: _wsToastErr(pushToast, "Rename failed"),
    }
  );

  return (
    <Modal
      title="Rename workspace"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={rename.loading}>Cancel</Btn>
          <Btn kind="primary" icon="check" disabled={rename.loading} onClick={() => rename.mutate({ name })}>
            {rename.loading ? "Saving…" : "Save"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">
          Name <span className="hint">for <span className="mono">{workspace.id}</span> · clear to remove the label</span>
        </label>
        <input
          className="input"
          style={{ width: "100%" }}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Investing research"
          autoFocus
          onKeyDown={(e) => { if (e.key === "Enter" && !rename.loading) rename.mutate({ name }); }}
        />
      </div>
    </Modal>
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
  const [name, setName] = React.useState("");
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
    const body = { template_id: templateId };
    if (name.trim()) body.name = name.trim();
    create.mutate(body);
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
        <label className="field-label">Name <span className="hint">optional - a human-readable label</span></label>
        <input
          className="input"
          style={{ width: "100%" }}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Investing research"
        />
      </div>
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

const WS_TABS = ["files", "sessions", "events", "log", "channels", "config", "destroy"];

function WorkspaceDetail({ workspaceId, onOpenSession, onNavigate, pushToast }) {
  const { useResource, useRouter, useViewport, apiFetch } = window.primerApi;
  const { params, query, navigate } = useRouter();
  const { isMobile } = useViewport();
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
    { id: "events", label: "Events", icon: "zap" },
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

  // Lifted tab-panel JSX — shared by the desktop tab strip and the mobile
  // MobileTabs renderer below. Keeping panels in named locals avoids
  // duplicating the wid/onOpen wiring across both views.
  const filesPanel = <WS_FilesTab wid={wid} pushToast={pushToast} />;
  const sessionsPanel = <WS_SessionsTab wid={wid} onOpen={onOpenSession} />;
  const eventsPanel = <window.WorkspaceTap wid={wid} />;
  const logPanel = <WS_LogTab wid={wid} />;
  const channelsPanel = <WS_ChannelsTab wid={wid} ws={ws} pushToast={pushToast} />;
  const configPanel = <WS_ConfigTab wid={wid} ws={ws} />;
  const destroyPanel = <WS_DestroyTab wid={wid} pushToast={pushToast} sessionsForBadge={sessionsForBadge} />;

  // Mobile tab id "logs" maps onto the desktop "log" tab content so the
  // mobile/desktop label stays singular in routes while the spec-required
  // detail tab id remains "logs".
  const mobileTabs = [
    { id: "files", label: "Files", content: filesPanel },
    { id: "sessions", label: "Sessions", content: sessionsPanel },
    { id: "logs", label: "Logs", content: logPanel },
    { id: "config", label: "Config", content: configPanel },
  ];
  const mobileActive = query.tab === "logs" ? "logs"
    : (mobileTabs.find((t) => t.id === tab) ? tab : "files");

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
      {isMobile ? (
        <MobileTabs
          tabs={mobileTabs}
          active={mobileActive}
          onSelect={(id) => setTab(id === "logs" ? "log" : id)}
        />
      ) : (
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
          {tab === "files" && filesPanel}
          {tab === "sessions" && sessionsPanel}
          {tab === "events" && eventsPanel}
          {tab === "log" && logPanel}
          {tab === "channels" && channelsPanel}
          {tab === "config" && configPanel}
          {tab === "destroy" && destroyPanel}
        </div>
      </div>
      )}
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
  // Tree-wide delete confirm: { path, isDir } | null. A directory delete
  // is recursive (removes its contents), so the modal warns accordingly.
  const [pendingDelete, setPendingDelete] = React.useState(null);
  // Create modal: "file" | "dir" | null, plus its draft fields.
  const [createMode, setCreateMode] = React.useState(null);
  const [createPath, setCreatePath] = React.useState("");
  const [createContent, setCreateContent] = React.useState("");
  // "raw" | "rendered". Only meaningful for files where the renderer
  // can do something useful (today: .md). For everything else the
  // toggle isn't shown and this stays at "raw".
  const [viewMode, setViewMode] = React.useState("raw");
  // Reset to "rendered" whenever a fresh markdown file is selected —
  // that's the natural default for .md (the operator can flip to raw
  // if they want the source). For non-md files force "raw" so the
  // toggle state doesn't leak between selections.
  React.useEffect(() => {
    if (selected && selected.toLowerCase().endsWith(".md")) {
      setViewMode("rendered");
    } else {
      setViewMode("raw");
    }
  }, [selected]);

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

  // Delete any tree entry (file or directory). Directory deletes pass
  // recursive=true so a populated folder is removed with its contents.
  const deleteEntry = useMutation(
    ({ path, recursive }) => apiFetch(
      "DELETE",
      `/workspaces/${encodeURIComponent(wid)}/files?path=${encodeURIComponent(path)}` +
        (recursive ? "&recursive=true" : "")
    ),
    {
      invalidates: [`workspace-files:${wid}`],
      onSuccess: () => {
        const removed = pendingDelete;
        setPendingDelete(null);
        // Clear the editor if the deleted path was open (a file, or a
        // directory that contained the open file).
        if (selected && removed && (selected === removed.path || selected.startsWith(removed.path + "/"))) {
          setSelected(null);
          setEditing(false);
        }
        if (typeof pushToast === "function") {
          pushToast({
            kind: "warning",
            title: removed?.isDir ? "Folder deleted" : "File deleted",
            detail: removed?.path,
          });
        }
      },
      onError: _wsToastErr(pushToast, "Delete failed"),
    }
  );

  const createFileMut = useMutation(
    ({ path, content }) => apiFetch(
      "PUT",
      `/workspaces/${encodeURIComponent(wid)}/files?path=${encodeURIComponent(path)}`,
      { content, encoding: "text" }
    ),
    {
      invalidates: [`workspace-files:${wid}`],
      onSuccess: () => {
        const created = createPath;
        _wsExpandParents(setOpenDirs, created);
        setCreateMode(null);
        setSelected(created);
        setEditing(false);
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "File created", detail: created });
        }
      },
      onError: _wsToastErr(pushToast, "Create failed"),
    }
  );

  const makeDirMut = useMutation(
    ({ path }) => apiFetch(
      "POST",
      `/workspaces/${encodeURIComponent(wid)}/files/dir?path=${encodeURIComponent(path)}`
    ),
    {
      invalidates: [`workspace-files:${wid}`],
      onSuccess: () => {
        const created = createPath;
        _wsExpandParents(setOpenDirs, created);
        setOpenDirs((prev) => new Set(prev).add(created));
        setCreateMode(null);
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Folder created", detail: created });
        }
      },
      onError: _wsToastErr(pushToast, "Create failed"),
    }
  );

  const selectFile = (path) => {
    setSelected(path);
    setEditing(false);
  };

  const requestDelete = (path, isDir) => setPendingDelete({ path, isDir });

  const openCreate = (mode) => {
    setCreateMode(mode);
    setCreatePath("");
    setCreateContent("");
  };

  const onSubmitCreate = () => {
    const path = createPath.trim().replace(/^\/+/, "");
    if (!path) return;
    if (createMode === "dir") {
      makeDirMut.mutate({ path });
    } else {
      createFileMut.mutate({ path, content: createContent });
    }
  };

  const onSave = () => {
    if (!selected) return;
    saveFile.mutate({ content: draft, encoding: "text" });
  };

  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "300px 1fr",
      // Bound the grid to the viewport so the tree pane + the editor
      // pane both scroll internally (`overflow: auto` below) instead
      // of pushing the page-level scrollbar. Without this, opening
      // a long markdown file expanded the grid to fit the rendered
      // content and the whole page scrolled. Matches the pattern
      // chats.jsx uses for its chat-detail pane. The 220px offset
      // covers the global topbar + the workspace page-header + the
      // tab strip; minHeight keeps a reasonable floor on tiny viewports.
      height: "calc(100vh - 220px)",
      minHeight: 480,
      fontSize: 12.5,
    }}>
      {/* Tree */}
      <div style={{ borderRight: "1px solid var(--border)", overflow: "auto", padding: "10px 0", minHeight: 0 }}>
        <div style={{ display: "flex", alignItems: "center", padding: "0 12px 8px", gap: 6 }}>
          <span className="mono muted text-sm">/ root</span>
          <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
            <button className="icon-btn" style={{ width: 22, height: 22 }} title="New file" data-testid="ws-new-file" onClick={() => openCreate("file")}><Icon name="doc" size={11} /></button>
            <button className="icon-btn" style={{ width: 22, height: 22 }} title="New folder" data-testid="ws-new-folder" onClick={() => openCreate("dir")}><Icon name="box" size={11} /></button>
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
            onDelete={requestDelete}
            rootEntries={tree.data?.items ?? []}
          />
        )}
      </div>

      {/* Editor pane */}
      <div style={{ display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0, minHeight: 0 }}>
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
                    {selected && selected.toLowerCase().endsWith(".md") && (
                      <Btn
                        size="sm"
                        kind="ghost"
                        onClick={() => setViewMode((m) => (m === "rendered" ? "raw" : "rendered"))}
                        title={viewMode === "rendered" ? "Show raw markdown source" : "Render the markdown"}
                        data-testid="ws-file-md-toggle"
                      >
                        {viewMode === "rendered" ? "Raw" : "Rendered"}
                      </Btn>
                    )}
                    <a
                      href={`/v1/workspaces/${encodeURIComponent(wid)}/files/download?path=${encodeURIComponent(selected)}`}
                      download
                      style={{ textDecoration: "none" }}
                    >
                      <Btn size="sm" kind="ghost" icon="external">Download</Btn>
                    </a>
                    <Btn size="sm" kind="ghost" icon="copy" onClick={() => setEditing(true)}>Edit</Btn>
                    <Btn
                      size="sm"
                      kind="danger"
                      icon="trash"
                      disabled={deleteEntry.loading}
                      onClick={() => requestDelete(selected, false)}
                    >
                      Delete
                    </Btn>
                  </>
                )}
              </>
            )}
          </div>
        </div>
        {pendingDelete && (
          <Modal
            title={`Delete ${pendingDelete.path}?`}
            danger
            onClose={() => setPendingDelete(null)}
            footer={
              <>
                <Btn kind="ghost" onClick={() => setPendingDelete(null)}>Cancel</Btn>
                <Btn
                  kind="danger"
                  icon="trash"
                  disabled={deleteEntry.loading}
                  onClick={() => deleteEntry.mutate({ path: pendingDelete.path, recursive: pendingDelete.isDir })}
                >
                  {pendingDelete.isDir ? "Delete folder" : "Delete file"}
                </Btn>
              </>
            }
          >
            <p>
              {pendingDelete.isDir
                ? "This removes the folder and everything inside it from the workspace. The operation cannot be undone from the console."
                : "This removes the file from the workspace. The operation cannot be undone from the console."}
            </p>
          </Modal>
        )}
        {createMode && (
          <Modal
            title={createMode === "dir" ? "New folder" : "New file"}
            onClose={() => setCreateMode(null)}
            footer={
              <>
                <Btn kind="ghost" onClick={() => setCreateMode(null)}>Cancel</Btn>
                <Btn
                  kind="primary"
                  icon="check"
                  disabled={!createPath.trim() || createFileMut.loading || makeDirMut.loading}
                  onClick={onSubmitCreate}
                >
                  {createMode === "dir" ? "Create folder" : "Create file"}
                </Btn>
              </>
            }
          >
            <label className="text-sm muted" style={{ display: "block", marginBottom: 4 }}>
              {createMode === "dir" ? "Folder path (relative to workspace root)" : "File path (relative to workspace root)"}
            </label>
            <input
              className="input mono"
              autoFocus
              data-testid="ws-create-path"
              placeholder={createMode === "dir" ? "e.g. src/utils" : "e.g. src/main.py"}
              value={createPath}
              onChange={(e) => setCreatePath(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && createMode === "dir") onSubmitCreate(); }}
              style={{ width: "100%" }}
            />
            {createMode === "file" && (
              <>
                <label className="text-sm muted" style={{ display: "block", margin: "12px 0 4px" }}>
                  Initial contents (optional)
                </label>
                <textarea
                  className="textarea mono"
                  data-testid="ws-create-content"
                  value={createContent}
                  onChange={(e) => setCreateContent(e.target.value)}
                  style={{ width: "100%", minHeight: 160, fontSize: 12, lineHeight: 1.55 }}
                />
              </>
            )}
            <p className="text-sm muted" style={{ marginTop: 10 }}>
              Parent folders are created automatically. Paths under <span className="mono">.state</span> / <span className="mono">.tmp</span> are reserved.
            </p>
          </Modal>
        )}
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
          ) : selected && selected.toLowerCase().endsWith(".md") && viewMode === "rendered" ? (
            <div
              className="md-rendered"
              style={{ padding: 14, fontSize: 13, lineHeight: 1.6, color: "var(--text)" }}
              data-testid="ws-file-md-rendered"
            >
              {typeof window.renderMarkdown === "function"
                ? window.renderMarkdown(fileContent.data?.content || "")
                : (
                  <pre className="mono" style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                    {fileContent.data?.content || ""}
                  </pre>
                )}
            </div>
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

function WS_DirNode({ wid, path, depth, openDirs, toggleDir, selected, selectFile, onDelete, rootEntries }) {
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
          onDelete={onDelete}
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
              onDelete={onDelete}
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
            onDelete={onDelete}
          />
        );
      })}
    </div>
  );
}

function WS_FileRow({ path, name, depth, isDir, open, isSelected, system, onClick, size, onDelete }) {
  const [hover, setHover] = React.useState(false);
  // Reserved/backend-managed entries are not deletable from the console.
  const canDelete = !system && typeof onDelete === "function";
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
      onMouseEnter={(e) => { setHover(true); if (!isSelected) e.currentTarget.style.background = "var(--bg-hover)"; }}
      onMouseLeave={(e) => { setHover(false); if (!isSelected) e.currentTarget.style.background = "transparent"; }}
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
      {!isDir && size != null && !(hover && canDelete) && (
        <span className="muted mono text-sm" style={{ marginLeft: "auto", fontSize: 10.5 }}>
          {size === 0 ? "0" : size < 1024 ? `${size}B` : `${(size / 1024).toFixed(1)}K`}
        </span>
      )}
      {canDelete && hover && (
        <button
          className="icon-btn"
          style={{ marginLeft: "auto", width: 18, height: 18 }}
          title={isDir ? "Delete folder" : "Delete file"}
          data-testid={`ws-row-delete:${path}`}
          onClick={(e) => { e.stopPropagation(); onDelete(path, !!isDir); }}
        >
          <Icon name="trash" size={10} />
        </button>
      )}
    </div>
  );
}

// Expand every ancestor directory of `path` in the tree so a freshly
// created file/folder is revealed without the operator hand-expanding.
function _wsExpandParents(setOpenDirs, path) {
  const parts = (path || "").split("/").filter(Boolean);
  const ancestors = [];
  for (let i = 1; i < parts.length; i++) {
    ancestors.push(parts.slice(0, i).join("/"));
  }
  if (!ancestors.length) return;
  setOpenDirs((prev) => {
    const next = new Set(prev);
    ancestors.forEach((a) => next.add(a));
    return next;
  });
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
  const [openSha, setOpenSha] = React.useState(null);

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
          <span className="mono">git log</span> of <span className="mono">.state</span> · click a row to see the diff
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
            {commits.map((e) => (
              <WS_LogRow
                key={e.sha}
                wid={wid}
                commit={e}
                expanded={openSha === e.sha}
                onToggle={() => setOpenSha(openSha === e.sha ? null : e.sha)}
              />
            ))}
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

function WS_LogRow({ wid, commit, expanded, onToggle }) {
  const { useResource, apiFetch } = window.primerApi;
  const sha = (commit.sha || "").slice(0, 7);
  const at = commit.committed_at;
  const sec = at ? _wsAgeSec(at) : null;
  // Op chip colour by family.
  const opColor = (
    commit.op === "message" ? "var(--blue)"
    : commit.op === "user_instruction" ? "var(--accent)"
    : commit.op === "status_change" ? "var(--amber)"
    : commit.op === "attach" ? "var(--text-3)"
    : commit.op === "tool" ? "var(--violet)"
    : "var(--text-3)"
  );
  const sessionShort = commit.session_id ? commit.session_id.slice(-10) : null;

  return (
    <div style={{ position: "relative", padding: "4px 0", marginBottom: 2 }}>
      <div style={{ position: "absolute", left: -4, top: 11, width: 11, height: 11, borderRadius: "50%", background: "var(--bg-2)", border: "2px solid var(--accent)" }}></div>
      <div
        onClick={onToggle}
        className="touch-target"
        style={{
          display: "flex", alignItems: "center", gap: 10, padding: "6px 8px 6px 16px",
          cursor: "pointer", borderRadius: 4,
          background: expanded ? "var(--bg-hover)" : "transparent",
        }}
        title={`Click to ${expanded ? "collapse" : "expand"} the diff`}
      >
        <Icon name={expanded ? "chevron-down" : "chevron-right"} size={11} className="muted" />
        <span className="mono" style={{ color: "var(--accent)", fontSize: 12, fontWeight: 500 }}>{sha}</span>
        {commit.op && (
          <span
            className="mono"
            style={{
              fontSize: 10, padding: "1px 6px", borderRadius: 3,
              background: "var(--bg-2)", color: opColor, border: `1px solid ${opColor}`,
            }}
          >{commit.op}</span>
        )}
        {commit.agent_id && (
          <span className="mono text-sm" style={{ color: "var(--text-2)" }}>
            <Icon name="agent" size={10} style={{ display: "inline", verticalAlign: "-1px" }} /> {commit.agent_id}
          </span>
        )}
        {sessionShort && (
          <span className="mono text-sm muted" title={commit.session_id}>
            {sessionShort}
          </span>
        )}
        {sec != null && <span className="muted mono text-sm">{relativeTime(sec)}</span>}
        <span style={{ fontSize: 12.5, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {commit.subject || ""}
        </span>
      </div>
      {expanded && <WS_CommitDiff wid={wid} sha={commit.sha} />}
    </div>
  );
}

function WS_CommitDiff({ wid, sha }) {
  const { useResource, apiFetch } = window.primerApi;
  const diff = useResource(
    `workspace-commit:${wid}:${sha}`,
    (signal) => apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/commit/${encodeURIComponent(sha)}`, null, { signal }),
    { deps: [wid, sha] }
  );

  if (diff.loading && !diff.data) {
    return (
      <div className="muted text-sm" style={{ padding: "8px 28px" }}>Loading diff…</div>
    );
  }
  if (diff.error) {
    return (
      <div style={{ padding: "8px 28px" }}>
        <Banner
          kind="error"
          title={diff.error.title || "Couldn't load diff"}
          detail={diff.error.detail || diff.error.message}
        />
      </div>
    );
  }
  const files = diff.data?.files || [];
  if (files.length === 0) {
    return (
      <div className="muted text-sm" style={{ padding: "8px 28px" }}>
        Trailer-only commit — no file changes.
      </div>
    );
  }
  return (
    <div style={{ padding: "6px 28px 10px", display: "flex", flexDirection: "column", gap: 8 }}>
      {diff.data?.body && (
        <pre className="mono text-sm muted" style={{ margin: 0, whiteSpace: "pre-wrap" }}>
          {diff.data.body}
        </pre>
      )}
      {files.map((f) => (
        <div key={f.path} style={{ border: "1px solid var(--border)", borderRadius: 4 }}>
          <div style={{ padding: "4px 8px", background: "var(--bg-2)", display: "flex", alignItems: "center", gap: 8, borderBottom: "1px solid var(--border)" }}>
            <span
              className="mono"
              style={{
                fontSize: 10, padding: "0 5px", borderRadius: 2,
                background: "var(--bg-1)",
                color: (
                  f.status === "A" ? "var(--green)"
                  : f.status === "D" ? "var(--red)"
                  : f.status === "M" ? "var(--blue)"
                  : "var(--text-3)"
                ),
              }}
            >{f.status}</span>
            <span className="mono text-sm">{f.path}</span>
          </div>
          {f.patch ? (
            <pre className="mono text-sm" style={{ margin: 0, padding: 8, overflow: "auto", maxHeight: 360, background: "var(--bg-1)" }}>
              {f.patch.split("\n").map((line, i) => {
                let color = "var(--text-2)";
                if (line.startsWith("+") && !line.startsWith("+++")) color = "var(--green)";
                else if (line.startsWith("-") && !line.startsWith("---")) color = "var(--red)";
                else if (line.startsWith("@@")) color = "var(--violet)";
                return (
                  <div key={i} style={{ color }}>{line || " "}</div>
                );
              })}
            </pre>
          ) : (
            <div className="muted text-sm" style={{ padding: 8 }}>(binary or no diff)</div>
          )}
        </div>
      ))}
    </div>
  );
}

// ============================================================================
// Channels tab - workspace-owned reply binding
// ============================================================================

function WS_ChannelsTab({ wid, ws, pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const [showLink, setShowLink] = React.useState(false);

  // The standing reply binding lives on the workspace row itself.
  const wsData = ws.data || {};
  const currentBinding = wsData.reply_binding || null;
  const linkedChannelId = currentBinding?.channel_id || null;
  const boundAnchor = currentBinding?.anchor || null;

  // Fetch channels list for display (label) + link picker
  const channelsList = useResource(
    "workspace-channels-options",
    (signal) => apiFetch("GET", "/channels?limit=200", null, { signal }),
    {}
  );
  const channels = channelsList.data?.items ?? [];

  const linkedChannel = linkedChannelId
    ? channels.find((c) => c.id === linkedChannelId) || null
    : null;

  const unlink = useMutation(
    () => apiFetch("DELETE", `/workspaces/${encodeURIComponent(wid)}/reply_binding`),
    {
      invalidates: [`workspace-detail:${wid}`],
      onSuccess: () => {
        if (typeof pushToast === "function") {
          pushToast({ kind: "warning", title: "Reply binding cleared" });
        }
        ws.refetch && ws.refetch();
      },
      onError: _wsToastErr(pushToast, "Unlink failed"),
    }
  );

  return (
    <div style={{ padding: 14 }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
        <div>
          <div className="text-sm" style={{ fontWeight: 600 }}>Reply binding</div>
          <div className="text-sm muted">
            The standing outbound channel for this workspace's session gates
            (ask_user / approvals / inform_user / final result).
          </div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          {linkedChannelId && (
            <Btn
              size="sm"
              kind="danger"
              icon="x"
              disabled={unlink.loading}
              onClick={() => unlink.mutate()}
            >
              {unlink.loading ? "Unlinking…" : "Unlink"}
            </Btn>
          )}
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowLink(true)}>
            {linkedChannelId ? "Change channel" : "Link channel"}
          </Btn>
        </div>
      </div>

      {ws.error && !wsData.id ? (
        <Banner
          kind="error"
          title={ws.error.title || "Couldn't load workspace"}
          detail={ws.error.detail || ws.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={ws.refetch}>Retry</Btn>}
        />
      ) : !linkedChannelId ? (
        <div className="empty">
          <div className="ico-wrap"><Icon name="bell" size={18} /></div>
          <div className="head">No reply binding set</div>
          <div className="sub">Bind an external channel (Slack, Telegram, Discord) to forward this workspace's session gates and final results to that channel.</div>
          <div className="actions">
            <Btn size="sm" kind="primary" icon="plus" onClick={() => setShowLink(true)}>Link channel</Btn>
          </div>
        </div>
      ) : (
        <div className="panel">
          <div className="panel-body">
            <dl className="kv" style={{ gridTemplateColumns: "140px 1fr" }}>
              <dt>channel id</dt>
              <dd className="mono">{linkedChannelId}</dd>
              {boundAnchor && (
                <>
                  <dt>anchor</dt>
                  <dd className="mono">{boundAnchor}</dd>
                </>
              )}
              {linkedChannel && linkedChannel.label && (
                <>
                  <dt>label</dt>
                  <dd>{linkedChannel.label}</dd>
                </>
              )}
              {linkedChannel && linkedChannel.provider_id && (
                <>
                  <dt>provider</dt>
                  <dd className="mono">{linkedChannel.provider_id}</dd>
                </>
              )}
            </dl>
          </div>
        </div>
      )}

      {showLink && (
        <WS_LinkChannelModal
          wid={wid}
          channels={channels}
          onClose={() => setShowLink(false)}
          onLinked={() => { ws.refetch && ws.refetch(); }}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}

function WS_LinkChannelModal({ wid, channels, onClose, onLinked, pushToast }) {
  const { useMutation, apiFetch } = window.primerApi;
  const [channelId, setChannelId] = React.useState(channels[0]?.id || "");
  const [anchor, setAnchor] = React.useState("");

  const link = useMutation(
    (body) => apiFetch("PUT", `/workspaces/${encodeURIComponent(wid)}/reply_binding`, body),
    {
      invalidates: [`workspace-detail:${wid}`],
      onSuccess: () => {
        onClose();
        if (typeof onLinked === "function") onLinked();
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Reply binding set" });
        }
      },
      onError: _wsToastErr(pushToast, "Link failed"),
    }
  );

  const onLink = () => {
    if (!channelId) return;
    link.mutate({ channel_id: channelId, anchor: anchor || null });
  };

  return (
    <Modal
      title="Link channel"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="check" disabled={!channelId || link.loading} onClick={onLink}>
            {link.loading ? "Linking…" : "Link channel"}
          </Btn>
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
            {channels.map((c) => (
              <option key={c.id} value={c.id}>
                {c.id}{c.label ? ` · ${c.label}` : ""}
              </option>
            ))}
          </select>
        )}
      </div>
      <div className="field" style={{ marginTop: 12 }}>
        <label className="field-label">anchor <span className="hint">optional</span></label>
        <input
          className="input mono"
          style={{ width: "100%" }}
          value={anchor}
          placeholder="room / thread id"
          onChange={(e) => setAnchor(e.target.value)}
        />
        <div className="muted text-sm" style={{ marginTop: 4 }}>
          optional - the default room/thread to post into
        </div>
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
// Reusable detail panels — surfaced to window.* so the Studio's Workspace
// Settings overlay (studio-settings.jsx) can render the SAME components for
// the orphaned channels/reply-binding · config · git-log · destroy features
// without reimplementing them. WorkspaceDetail continues to reference these
// via their in-file bindings; the exports are additive.
window.WS_ChannelsTab = WS_ChannelsTab;
window.WS_ConfigTab = WS_ConfigTab;
window.WS_LogTab = WS_LogTab;
window.WS_DestroyTab = WS_DestroyTab;
