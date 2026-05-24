/* global React, Icon, StatusPill, Btn, Modal, Banner, relativeTime, fmtDate */

const { apiFetch, useResource, useMutation, useRouter, useToast } = window.matrixApi;

// ============================================================================
// List page
// ============================================================================

function WorkspacesPage() {
  const { query: routerQuery, navigate } = useRouter();
  const { push: pushToast } = useToast();

  // Open create modal when navigated with ?create=1 (set by the
  // Dashboard quick action). Strip the param after opening so a
  // refresh doesn't reopen it.
  const [createOpen, setCreateOpen] = React.useState(false);
  React.useEffect(() => {
    if (routerQuery.create === "1") {
      setCreateOpen(true);
      const rest = { ...routerQuery };
      delete rest.create;
      navigate("/workspaces", rest);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const list = useResource("workspaces:list",
    (s) => apiFetch("GET", "/workspaces?limit=200", null, { signal: s }), {});
  const templates = useResource("workspaces:templates",
    (s) => apiFetch("GET", "/workspace_templates?limit=200", null, { signal: s }), {});

  // Per-row session count: batched on every list settle. One fetch per
  // workspace row → /v1/workspaces/{id}/sessions?limit=1. Acceptable
  // for the v1 row count; if the workspace count grows beyond ~50 a
  // backend aggregate would be worth adding.
  const [perWsSessions, setPerWsSessions] = React.useState({});
  React.useEffect(() => {
    const items = list.data?.items;
    if (!items || items.length === 0) return undefined;
    const ctrl = new AbortController();
    Promise.all(
      items.map((w) =>
        apiFetch(
          "GET",
          `/workspaces/${encodeURIComponent(w.id)}/sessions?limit=1`,
          null,
          { signal: ctrl.signal }
        )
          .then((r) => [w.id, r.total ?? 0])
          .catch(() => [w.id, null])
      )
    ).then((entries) => setPerWsSessions(Object.fromEntries(entries)));
    return () => ctrl.abort();
  }, [list.data]);

  const [textFilter, setTextFilter] = React.useState("");
  const [templateFilter, setTemplateFilter] = React.useState("");
  const items = list.data?.items ?? [];
  const filtered = items.filter((w) => {
    if (templateFilter && w.template_id !== templateFilter) return false;
    if (textFilter) {
      const q = textFilter.toLowerCase();
      if (!w.id.toLowerCase().includes(q) && !(w.template_id || "").toLowerCase().includes(q)) return false;
    }
    return true;
  });

  return (
    <div className="col" style={{ gap: 14 }}>
      <WorkspacesHeader count={items.length} onRefresh={list.refetch} />

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter workspaces…"
            value={textFilter}
            onChange={(e) => setTextFilter(e.target.value)}
          />
        </div>
        <div className="sep-v" />
        <select className="select" value={templateFilter} onChange={(e) => setTemplateFilter(e.target.value)}>
          <option value="">all templates</option>
          {(templates.data?.items ?? []).map((t) => <option key={t.id} value={t.id}>{t.id}</option>)}
        </select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New workspace</Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Template</th>
              <th style={{ textAlign: "right" }}>Sessions</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {list.loading && items.length === 0 ? (
              <tr><td colSpan={5} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading workspaces…</td></tr>
            ) : list.error && items.length === 0 ? (
              <tr><td colSpan={5} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{list.error.title || list.error.message}</span>
                {" · "}<a onClick={list.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              items.length === 0 ? (
                <tr><td colSpan={5}>
                  <div className="empty" style={{ padding: "40px 20px" }}>
                    <div className="ico-wrap"><Icon name="box" size={22} /></div>
                    <div className="head">No workspaces yet</div>
                    <div className="sub">A workspace is a sandboxed filesystem + exec environment created from a template.</div>
                    <div className="actions"><Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New workspace</Btn></div>
                  </div>
                </td></tr>
              ) : (
                <tr><td colSpan={5} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                  No workspaces match the current filter.
                </td></tr>
              )
            ) : filtered.map((w) => {
              const sessionCount = perWsSessions[w.id];
              return (
                <tr key={w.id} onClick={() => navigate("/workspaces/" + w.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{w.id}</td>
                  <td className="mono">{w.template_id || <span className="muted">—</span>}</td>
                  <td className="mono num tabular">
                    {sessionCount == null
                      ? <span className="muted">…</span>
                      : sessionCount > 0
                        ? <span style={{ color: "var(--blue)" }}>{sessionCount}</span>
                        : <span className="muted">0</span>}
                  </td>
                  <td className="mono muted">{w.created_at ? relativeTime((Date.now() - new Date(w.created_at).getTime()) / 1000) : "—"}</td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}>
                    <Icon name="chevron-right" size={12} className="muted" />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <NewWorkspaceModal
          onClose={() => setCreateOpen(false)}
          onCreate={(w) => {
            setCreateOpen(false);
            pushToast({ kind: "success", title: "Workspace created", detail: w.id });
            navigate("/workspaces/" + w.id);
          }}
        />
      )}
    </div>
  );
}

function WorkspacesHeader({ count, onRefresh }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span style={{ color: "var(--text)" }}>Workspaces</span>
        </div>
        <h1 className="page-title">Workspaces</h1>
        <div className="page-sub tabular">
          {count} workspace{count === 1 ? "" : "s"}
          <span className="mono" style={{ marginLeft: 4, color: "var(--text-3)" }}>· manual refresh</span>
        </div>
      </div>
      <div className="page-actions">
        <Btn icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>
      </div>
    </div>
  );
}

// ============================================================================
// Create modal
// ============================================================================

function NewWorkspaceModal({ onClose, onCreate }) {
  const templates = useResource("workspaces:templates",
    (s) => apiFetch("GET", "/workspace_templates?limit=200", null, { signal: s }), {});
  const [templateId, setTemplateId] = React.useState("");
  const [overrides, setOverrides] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState({});
  const { push: pushToast } = useToast();

  React.useEffect(() => {
    if (!templateId && templates.data?.items?.length) {
      setTemplateId(templates.data.items[0].id);
    }
  }, [templates.data, templateId]);

  const create = useMutation(
    (body) => apiFetch("POST", "/workspaces", body),
    {
      onSuccess: (w) => { onCreate && onCreate(w); },
      onError: (err) => {
        if (err.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) next[(fe.loc || []).join(".")] = fe.msg;
          setFieldErrors(next);
        } else {
          pushToast({ kind: "error", title: err.title || "Create failed", detail: err.detail || err.message, requestId: err.requestId });
        }
      },
      invalidates: ["workspaces:list"],
    }
  );

  const submit = async () => {
    setFieldErrors({});
    // Parse the optional overrides textarea as a relaxed key=value /
    // line-separated format. Skip parsing for v1 — empty payload is
    // sufficient to materialise from a default template. If user enters
    // text, ignore it for now and surface a note instead. (The full
    // overrides editor is a follow-on per Workspaces spec §3.2.)
    const body = { template_id: templateId };
    try { await create.mutate(body); } catch (_e) {}
  };

  return (
    <Modal
      title="New workspace"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit} disabled={!templateId || create.loading}>
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="banner banner-info" style={{ margin: "0 0 12px", fontSize: 11.5 }}>
        <Icon name="info" size={12} className="ico" />
        <div>
          ID is generated by the backend — any <span className="mono">id</span> field
          in this body is silently ignored (app spec §12). Form intentionally has no
          ID input.
        </div>
      </div>
      <div className="field">
        <label className="field-label">Template</label>
        <select
          className="select"
          value={templateId}
          onChange={(e) => setTemplateId(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="">-- pick a template --</option>
          {(templates.data?.items ?? []).map((t) => (
            <option key={t.id} value={t.id}>{t.id}{t.description ? ` — ${t.description}` : ""}</option>
          ))}
        </select>
        {templates.loading && <div className="field-help">Loading templates…</div>}
        {(templates.data?.items ?? []).length === 0 && !templates.loading && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No templates registered. Create a WorkspaceTemplate via the API first
            (POST /v1/workspace_templates).
          </div>
        )}
        {fieldErrors["body.template_id"] && (
          <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.template_id"]}</div>
        )}
      </div>
      <div className="field">
        <label className="field-label">Overrides <span className="hint">v2 — currently ignored</span></label>
        <textarea
          className="textarea mono"
          placeholder="env.MY_KEY=value (UI-side editor not yet implemented)"
          value={overrides}
          onChange={(e) => setOverrides(e.target.value)}
          rows={3}
          disabled
          style={{ opacity: 0.5 }}
        />
      </div>
    </Modal>
  );
}

// ============================================================================
// Detail page
// ============================================================================

const TABS = [
  { id: "files", label: "Files", icon: "doc" },
  { id: "sessions", label: "Sessions", icon: "zap" },
  { id: "log", label: "Log", icon: "git-commit" },
  { id: "config", label: "Config", icon: "settings" },
  { id: "destroy", label: "Destroy", icon: "trash", danger: true },
];

function WorkspaceDetail() {
  const { params, query: routerQuery, navigate } = useRouter();
  const wid = params.id;
  const tab = TABS.some((t) => t.id === routerQuery.tab) ? routerQuery.tab : "files";

  const ws = useResource(
    "workspace-detail:" + wid,
    (s) => apiFetch("GET", "/workspaces/" + encodeURIComponent(wid), null, { signal: s }),
    { pollMs: null, deps: [wid] }
  );

  // Live session count for the Sessions tab badge — small extra fetch
  // shared with the FilesTab.
  const sessionCount = useResource(
    "workspace-sessions-count:" + wid,
    (s) => apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/sessions?limit=1`, null, { signal: s }),
    { pollMs: 10000, deps: [wid] }
  );

  const setTab = (t) => navigate("/workspaces/" + wid, { tab: t });

  if (ws.loading && !ws.data) {
    return <>
      <WorkspaceHeader wid={wid} navigate={navigate} ws={null} />
      <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading workspace {wid}…</div>
    </>;
  }
  if (ws.error && !ws.data) {
    return <>
      <WorkspaceHeader wid={wid} navigate={navigate} ws={null} />
      <Banner
        kind="error"
        title={ws.error.title || "Couldn't load workspace"}
        detail={ws.error.detail || ws.error.message}
        actions={
          ws.error.status === 404
            ? <Btn size="sm" icon="chevron-left" onClick={() => navigate("/workspaces")}>Back to list</Btn>
            : <Btn size="sm" icon="refresh" onClick={ws.refetch}>Retry</Btn>
        }
      />
    </>;
  }
  const wsData = ws.data;

  return (
    <div className="col" style={{ gap: 14 }}>
      <WorkspaceHeader wid={wid} navigate={navigate} ws={wsData} />

      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {TABS.map((t) => {
            const count = t.id === "sessions" ? sessionCount.data?.total : null;
            return (
              <button
                key={t.id}
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
                {count != null && count > 0 && (
                  <span className="count" style={{ marginLeft: 4 }}>{count}</span>
                )}
              </button>
            );
          })}
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {tab === "files" && <FilesTab wid={wid} />}
          {tab === "sessions" && <SessionsTab wid={wid} />}
          {tab === "log" && <LogTab wid={wid} />}
          {tab === "config" && <ConfigTab ws={wsData} />}
          {tab === "destroy" && <DestroyTab wid={wid} sessionCount={sessionCount.data?.total} />}
        </div>
      </div>
    </div>
  );
}

function WorkspaceHeader({ wid, ws, navigate }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="crumb">
          <a onClick={() => navigate("/workspaces")}>Workspaces</a>
          <span className="sep">/</span>
          <span className="mono" style={{ color: "var(--text)" }}>{wid}</span>
        </div>
        <h1 className="page-title mono">{wid}</h1>
        {ws && (
          <div className="page-sub">
            <span className="mono">{ws.template_id || "(no template)"}</span>
            {ws.created_at && <> · <span className="muted">created {relativeTime((Date.now() - new Date(ws.created_at).getTime()) / 1000)}</span></>}
          </div>
        )}
      </div>
      <div className="page-actions">
        <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/workspaces")}>Back to list</Btn>
      </div>
    </div>
  );
}

// ============================================================================
// Files tab — lazy tree + editor with PUT save
// ============================================================================

function FilesTab({ wid }) {
  const [selected, setSelected] = React.useState(null);  // selected file path

  return (
    <div style={{ display: "grid", gridTemplateColumns: "300px 1fr", minHeight: 480, fontSize: 12.5 }}>
      <div style={{ borderRight: "1px solid var(--border)", overflow: "auto", padding: "10px 0" }}>
        <div style={{ display: "flex", alignItems: "center", padding: "0 12px 8px", gap: 6 }}>
          <span className="mono muted text-sm">/ root</span>
        </div>
        <DirectoryNode wid={wid} path="." depth={0} selected={selected} setSelected={setSelected} alwaysOpen />
      </div>
      <FileEditor wid={wid} selected={selected} />
    </div>
  );
}

function DirectoryNode({ wid, path, depth, selected, setSelected, alwaysOpen }) {
  const [open, setOpen] = React.useState(!!alwaysOpen);
  const cacheKey = `ws-files:${wid}:${path}`;
  // Only fetch when open. We do this by passing a noop fetcher when
  // closed — useResource still mounts but never fires.
  const dir = useResource(
    cacheKey,
    (s) => open
      ? apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/files?path=${encodeURIComponent(path)}`, null, { signal: s })
      : Promise.resolve({ items: [], total: 0 }),
    { pollMs: null, deps: [wid, path, open] }
  );

  const items = (dir.data?.items ?? []).slice();
  // Sort: directories first, then files. .state/.tmp to the bottom.
  items.sort((a, b) => {
    const aDir = a.kind === "dir";
    const bDir = b.kind === "dir";
    if (aDir && !bDir) return -1;
    if (!aDir && bDir) return 1;
    const aSys = _isSystemPath(a.path);
    const bSys = _isSystemPath(b.path);
    if (aSys && !bSys) return 1;
    if (!aSys && bSys) return -1;
    return a.path.localeCompare(b.path);
  });

  // For path !== ".": render a clickable directory row + (when open) its children.
  if (path !== ".") {
    const name = path.split("/").pop();
    return (
      <div>
        <FileRow
          name={name}
          depth={depth - 1}
          isDir
          open={open}
          system={_isSystemPath(path)}
          onClick={() => setOpen((o) => !o)}
        />
        {open && (dir.loading
          ? <div className="muted text-sm" style={{ paddingLeft: 12 + depth * 14 + 22 }}>…</div>
          : dir.error
            ? <div className="text-sm" style={{ paddingLeft: 12 + depth * 14 + 22, color: "var(--red)" }}>{dir.error.title || dir.error.message}</div>
            : items.map((item) => (
              <FileTreeEntry
                key={item.path}
                wid={wid}
                entry={item}
                depth={depth}
                selected={selected}
                setSelected={setSelected}
              />
            ))
        )}
      </div>
    );
  }
  // Root: no row, just children.
  return (
    <>
      {dir.loading && items.length === 0 && (
        <div className="muted text-sm" style={{ padding: "8px 12px" }}>Loading files…</div>
      )}
      {dir.error && (
        <div className="text-sm" style={{ padding: "8px 12px", color: "var(--red)" }}>
          {dir.error.title || dir.error.message}
          {" · "}<a onClick={dir.refetch} style={{ cursor: "pointer" }}>Retry</a>
        </div>
      )}
      {items.map((item) => (
        <FileTreeEntry
          key={item.path}
          wid={wid}
          entry={item}
          depth={depth}
          selected={selected}
          setSelected={setSelected}
        />
      ))}
      {!dir.loading && items.length === 0 && !dir.error && (
        <div className="muted text-sm" style={{ padding: "8px 12px" }}>(empty)</div>
      )}
    </>
  );
}

function FileTreeEntry({ wid, entry, depth, selected, setSelected }) {
  if (entry.kind === "dir") {
    return (
      <DirectoryNode wid={wid} path={entry.path} depth={depth + 1} selected={selected} setSelected={setSelected} />
    );
  }
  return (
    <FileRow
      name={entry.path.split("/").pop()}
      depth={depth}
      isSelected={entry.path === selected}
      onClick={() => setSelected(entry.path)}
      size={entry.size_bytes}
      system={_isSystemPath(entry.path)}
    />
  );
}

function FileRow({ name, depth, isDir, open, isSelected, system, onClick, size }) {
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
      title={system ? "backend-managed; editing disabled" : undefined}
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

function _isSystemPath(p) {
  return p === ".state" || p === ".tmp" || p.startsWith(".state/") || p.startsWith(".tmp/");
}

function FileEditor({ wid, selected }) {
  const { push: pushToast } = useToast();
  const [editing, setEditing] = React.useState(false);
  const [editValue, setEditValue] = React.useState("");

  const info = useResource(
    selected ? `ws-info:${wid}:${selected}` : "ws-info:none",
    (s) => selected
      ? apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/files/info?path=${encodeURIComponent(selected)}`, null, { signal: s })
      : Promise.resolve(null),
    { pollMs: null, deps: [wid, selected] }
  );
  const content = useResource(
    selected ? `ws-read:${wid}:${selected}` : "ws-read:none",
    (s) => selected
      ? apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/files/read?path=${encodeURIComponent(selected)}`, null, { signal: s })
      : Promise.resolve(null),
    { pollMs: null, deps: [wid, selected] }
  );

  // When the loaded content changes, sync the edit buffer so Discard
  // works (revert to last-loaded).
  React.useEffect(() => {
    if (content.data?.content != null) setEditValue(content.data.content);
    setEditing(false);
  }, [content.data]);

  const save = useMutation(
    (body) => apiFetch("PUT", `/workspaces/${encodeURIComponent(wid)}/files?path=${encodeURIComponent(selected)}`, body),
    {
      invalidates: selected ? [`ws-read:${wid}:${selected}`, `ws-info:${wid}:${selected}`] : [],
      onSuccess: () => {
        setEditing(false);
        pushToast({ kind: "success", title: "Saved", detail: selected });
      },
      onError: (err) => pushToast({ kind: "error", title: "Save failed", detail: err.detail || err.message, requestId: err.requestId }),
    }
  );

  const isSystem = selected && _isSystemPath(selected);
  const isText = content.data?.encoding === "text";
  const sysWarn = isSystem ? "Editing disabled — backend-managed (.state / .tmp)" : null;
  const isPython = selected && selected.endsWith(".py");

  if (!selected) {
    return (
      <div className="muted text-sm" style={{ display: "grid", placeItems: "center" }}>
        Pick a file from the tree to preview or edit.
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", padding: "8px 12px", borderBottom: "1px solid var(--border)", gap: 8 }}>
        <Icon name="doc" size={12} className="muted" />
        <span className="mono" style={{ fontSize: 12 }}>{selected}</span>
        {info.data?.size_bytes != null && (
          <span className="muted mono text-sm">· {info.data.size_bytes} bytes</span>
        )}
        {sysWarn && <span className="muted text-sm" style={{ color: "var(--amber)" }}>· {sysWarn}</span>}
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          {editing ? (
            <>
              <Btn size="sm" kind="ghost" onClick={() => { setEditing(false); setEditValue(content.data?.content ?? ""); }}>Discard</Btn>
              <Btn
                size="sm"
                kind="primary"
                icon="check"
                onClick={() => save.mutate({ content: editValue, encoding: "text" })}
                disabled={save.loading}
              >
                {save.loading ? "Saving…" : "Save"}
              </Btn>
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
              <Btn
                size="sm"
                kind="ghost"
                icon="copy"
                onClick={() => setEditing(true)}
                disabled={isSystem || !isText || !content.data}
                title={isSystem ? "Editing disabled" : !isText ? "Binary file — download instead" : ""}
              >Edit</Btn>
            </>
          )}
        </div>
      </div>
      <div style={{ flex: 1, overflow: "auto", background: "var(--bg)" }}>
        {content.loading && !content.data ? (
          <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
        ) : content.error ? (
          <div style={{ padding: 20, textAlign: "center" }}>
            <span style={{ color: "var(--red)" }}>{content.error.title || content.error.message}</span>
            {" · "}<a onClick={content.refetch} style={{ cursor: "pointer" }}>Retry</a>
          </div>
        ) : !isText ? (
          <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
            Binary file ({content.data?.size_bytes ?? "?"} bytes) · use Download.
          </div>
        ) : editing ? (
          <textarea
            className="textarea mono"
            style={{ width: "100%", border: 0, borderRadius: 0, height: "100%", minHeight: 400, background: "var(--bg)", fontSize: 12, lineHeight: 1.55, padding: 14 }}
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
          />
        ) : (
          <pre className="mono" style={{ margin: 0, padding: 14, fontSize: 12, lineHeight: 1.55, color: "var(--text-2)", whiteSpace: "pre-wrap" }}>
            <CodeHighlight code={content.data?.content || ""} lang={isPython ? "python" : "other"} />
          </pre>
        )}
      </div>
    </div>
  );
}

function CodeHighlight({ code, lang }) {
  const lines = window.matrixVendor.highlightPython(code, lang);
  return (
    <>
      {lines.map((html, i) => (
        <div key={i} style={{ display: "flex" }}>
          <span style={{ width: 36, color: "var(--text-4)", textAlign: "right", paddingRight: 12, userSelect: "none", flexShrink: 0 }}>{i + 1}</span>
          <span dangerouslySetInnerHTML={{ __html: html }} />
        </div>
      ))}
    </>
  );
}

// ============================================================================
// Sessions tab
// ============================================================================

function SessionsTab({ wid }) {
  const { navigate } = useRouter();
  const result = useResource(
    `workspace-sessions:${wid}`,
    (s) => apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/sessions?limit=200`, null, { signal: s }),
    { pollMs: 5000, deps: [wid] }
  );
  const items = result.data?.items ?? [];
  return (
    <div style={{ padding: 14 }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
        <div>
          <div className="text-sm muted">Sessions on this workspace</div>
        </div>
      </div>
      {result.loading && items.length === 0 ? (
        <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
      ) : items.length === 0 ? (
        <div className="empty" style={{ padding: "30px 20px" }}>
          <div className="ico-wrap"><Icon name="zap" size={18} /></div>
          <div className="head">No sessions</div>
          <div className="sub">Start a session from the Sessions list or use the New session quick action.</div>
        </div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead><tr><th>Status</th><th>Session</th><th>Bound</th><th>Turns</th><th>Last turn</th></tr></thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.session_id} onClick={() => navigate("/sessions/" + s.session_id)} style={{ cursor: "pointer" }}>
                  <td><StatusPill status={s.status} /></td>
                  <td className="mono">{s.session_id}</td>
                  <td className="mono">{s.agent_id || <span className="muted">—</span>}</td>
                  <td className="mono num tabular">{s.turn_count ?? 0}</td>
                  <td className="mono muted">{s.last_activity_at ? relativeTime((Date.now() - new Date(s.last_activity_at).getTime()) / 1000) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Log tab
// ============================================================================

function LogTab({ wid }) {
  const [limit, setLimit] = React.useState(50);
  const log = useResource(
    `workspace-log:${wid}:${limit}`,
    (s) => apiFetch("GET", `/workspaces/${encodeURIComponent(wid)}/log?limit=${limit}`, null, { signal: s }),
    { pollMs: null, deps: [wid, limit] }
  );
  const entries = log.data?.commits ?? [];
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        <span className="mono">git log</span> of the workspace's <span className="mono">.state</span> repository
        · default limit 50, max 500
      </div>
      {log.loading && entries.length === 0 ? (
        <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
      ) : log.error ? (
        <Banner kind="error" title={log.error.title || "Couldn't load log"} detail={log.error.detail || log.error.message} />
      ) : entries.length === 0 ? (
        <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
          No commits yet — workspace <span className="mono">.state</span> is initialised lazily on first session activity.
        </div>
      ) : (
        <div style={{ position: "relative", paddingLeft: 18 }}>
          <div style={{ position: "absolute", left: 6, top: 6, bottom: 6, width: 1, background: "var(--border)" }}></div>
          {entries.map((e, i) => (
            <div key={(e.sha || e.id || i) + "-" + i} style={{ position: "relative", padding: "6px 0 6px 16px", display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ position: "absolute", left: -4, top: 9, width: 11, height: 11, borderRadius: "50%", background: "var(--bg-2)", border: "2px solid var(--accent)" }}></div>
              <span className="mono" style={{ color: "var(--accent)", fontSize: 12, fontWeight: 500 }}>{(e.sha || "").slice(0, 7) || "—"}</span>
              <span className="muted mono text-sm">{e.at ? relativeTime((Date.now() - new Date(e.at).getTime()) / 1000) : ""}</span>
              <span style={{ fontSize: 12.5 }}>"{e.message || e.msg || "(no message)"}"</span>
            </div>
          ))}
        </div>
      )}
      <div style={{ marginTop: 14, display: "flex", justifyContent: "center" }}>
        {limit < 500 && (
          <Btn size="sm" kind="ghost" onClick={() => setLimit(Math.min(500, limit + 50))}>Load more</Btn>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// Config tab — read-only render of the workspace row + materialised template
// ============================================================================

function ConfigTab({ ws }) {
  const templateId = ws?.template_id;
  const template = useResource(
    templateId ? `workspace-template:${templateId}` : "workspace-template:none",
    (s) => templateId
      ? apiFetch("GET", "/workspace_templates/" + encodeURIComponent(templateId), null, { signal: s })
      : Promise.resolve(null),
    { pollMs: null, deps: [templateId] }
  );
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        Read-only view of the workspace row + the materialised template. The
        overrides editor is a follow-on.
      </div>
      <div className="code-block" style={{ maxHeight: 460, overflow: "auto" }}>
        <span className="com"># workspace</span>{"\n"}
        <pre style={{ margin: 0 }} dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(ws, null, 2)) }} />
        {"\n"}
        <span className="com"># template ({templateId || "(none)"})</span>{"\n"}
        {template.loading ? "loading…" : template.error
          ? <span style={{ color: "var(--red)" }}>{template.error.title || template.error.message}</span>
          : <pre style={{ margin: 0 }} dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(template.data, null, 2)) }} />}
      </div>
    </div>
  );
}

// ============================================================================
// Destroy tab — DELETE /workspaces/{id} with confirm
// ============================================================================

function DestroyTab({ wid, sessionCount }) {
  const { navigate } = useRouter();
  const { push: pushToast } = useToast();
  const [open, setOpen] = React.useState(false);
  const destroy = useMutation(
    () => apiFetch("DELETE", "/workspaces/" + encodeURIComponent(wid)),
    {
      invalidates: ["workspaces:list"],
      onSuccess: () => {
        pushToast({ kind: "warning", title: "Workspace destroyed", detail: wid });
        navigate("/workspaces");
      },
      onError: (err) => pushToast({ kind: "error", title: "Destroy failed", detail: err.detail || err.message, requestId: err.requestId }),
    }
  );

  return (
    <div style={{ padding: 14 }}>
      <Banner
        kind="error"
        title="Permanent action"
        detail="Destroying a workspace cancels any in-flight sessions, deletes the .state repo, and removes all files. This cannot be undone."
      />
      <div className="kv mt-3" style={{ gridTemplateColumns: "180px 1fr" }}>
        <dt>workspace</dt><dd>{wid}</dd>
        <dt>sessions on this workspace</dt><dd>{sessionCount ?? "—"}</dd>
      </div>
      <div className="mt-4">
        <Btn kind="danger" icon="trash" onClick={() => setOpen(true)} disabled={destroy.loading}>
          {destroy.loading ? "Destroying…" : "Destroy workspace"}
        </Btn>
      </div>
      {open && (
        <Modal
          title={`Destroy ${wid}?`}
          danger
          onClose={() => setOpen(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setOpen(false)}>Cancel</Btn>
              <Btn kind="danger" icon="trash" onClick={async () => {
                setOpen(false);
                try { await destroy.mutate(); } catch (_e) {}
              }}>Destroy permanently</Btn>
            </>
          }
        >
          <ul>
            <li><strong>{sessionCount ?? "Any"}</strong> session{(sessionCount ?? 0) === 1 ? "" : "s"} on this workspace will be cancelled before destroy.</li>
            <li>The <span className="mono">.state</span> git repo will be deleted permanently.</li>
            <li>Files written by sessions are removed.</li>
            <li>DELETE is idempotent on workspaces per app spec §12; calling again returns 204.</li>
            <li>This cannot be undone.</li>
          </ul>
        </Modal>
      )}
    </div>
  );
}

window.WorkspacesPage = WorkspacesPage;
window.WorkspaceDetail = WorkspaceDetail;
