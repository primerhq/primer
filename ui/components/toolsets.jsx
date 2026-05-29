/* global React, Icon, Btn, Modal, Banner, relativeTime */

// Toolsets page + detail wired to the real API. The Designer's mock-data
// scaffold was replaced in Phase 2 — every fetch goes through
// window.primerApi.{apiFetch, useResource, useMutation}. Cache-key convention
// follows other components: "toolsets:list", "toolset-detail:${tid}",
// "toolset-tools:${tid}" (also used by agents.jsx for the same per-toolset
// /tools fetch — single canonical key keeps invalidation in sync across
// pages), "toolsets:approval-policies".
//
// Babel-standalone shares the global scope across <script> tags so every
// top-level binding in this file is prefixed with TS_ to avoid name clashes
// with agents.jsx (AG_TABS), providers.jsx (PROVIDER_FIELDS), and
// workspaces.jsx (WS_TERMINAL).

// Kept as a hardcoded fallback for filtering user toolsets out of the user
// list; the Built-in cards now fetch /v1/toolsets/builtin dynamically.
const TS_BUILTIN_RESERVED_IDS = ["system", "workspaces", "search", "misc", "web"];

const TS_TABS = [
  { id: "config", label: "Config", icon: "settings" },
  { id: "tools", label: "Tools", icon: "tools" },
  { id: "sessions", label: "Sessions", icon: "zap" },
];

const TS_KIND_COLORS = {
  mcp_stdio: "var(--accent)",
  mcp_http: "var(--blue)",
  mcp_sse: "var(--violet)",
  web: "var(--amber)",
  system: "var(--text-2)",
};

function _tsToastErr(pushToast, fallbackTitle) {
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
// Unified toolsets list (built-in + user)
// ============================================================================

function ToolsetsPage({ pushToast }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  // /v1/tools is the merged catalogue: each entry has {id, builtin,
  // tagline, available, tools[]}. Cheaper than fetching /toolsets and
  // /toolsets/builtin separately, and gives us the same row shape for
  // both kinds.
  const catalogue = useResource(
    "toolsets:catalogue",
    (signal) => apiFetch("GET", "/tools", null, { signal }),
    { pollMs: null }
  );

  const [createOpen, setCreateOpen] = React.useState(false);
  const [textFilter, setTextFilter] = React.useState("");
  const [kindFilter, setKindFilter] = React.useState("");

  const items = catalogue.data?.items ?? [];
  const filtered = React.useMemo(() => {
    let arr = items;
    if (textFilter) {
      const q = textFilter.toLowerCase();
      arr = arr.filter((t) => (t.id || "").toLowerCase().includes(q)
        || (t.label || "").toLowerCase().includes(q));
    }
    if (kindFilter === "builtin") arr = arr.filter((t) => t.builtin);
    if (kindFilter === "user") arr = arr.filter((t) => !t.builtin);
    if (kindFilter === "available") arr = arr.filter((t) => t.available);
    return arr;
  }, [items, textFilter, kindFilter]);

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter toolsets…"
            value={textFilter}
            onChange={(e) => setTextFilter(e.target.value)}
          />
        </div>
        <div className="sep-v" />
        <select
          className="select"
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
        >
          <option value="">all kinds</option>
          <option value="builtin">built-in</option>
          <option value="user">user</option>
          <option value="available">available only</option>
        </select>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={catalogue.refetch}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New toolset</Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Kind</th>
              <th>Status</th>
              <th>Tools</th>
              <th>Tagline</th>
            </tr>
          </thead>
          <tbody>
            {catalogue.loading && items.length === 0 ? (
              <tr><td colSpan={5} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</td></tr>
            ) : catalogue.error && items.length === 0 ? (
              <tr><td colSpan={5} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{catalogue.error.title || catalogue.error.message}</span>
                {" · "}<a onClick={catalogue.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={5} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No toolsets match.</td></tr>
            ) : filtered.map((t) => {
              const kindLabel = t.builtin ? "built-in" : "user";
              const kindColor = t.builtin ? "var(--blue)" : "var(--accent)";
              return (
                <tr key={t.id} onClick={() => navigate("/toolsets/" + t.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{t.id}</td>
                  <td>
                    <span className="pill" style={{ color: kindColor, borderColor: "var(--border)", background: "var(--bg-2)" }}>
                      <span className="dot" style={{ background: kindColor }}></span>
                      {kindLabel}
                    </span>
                  </td>
                  <td>
                    {t.available ? (
                      <span className="pill pill-ended"><span className="dot"></span>available</span>
                    ) : (
                      <span
                        className="pill pill-cancelled"
                        title={t.unavailable_reason || ""}
                      ><span className="dot"></span>unavailable</span>
                    )}
                  </td>
                  <td className="mono muted text-sm tabular num">{(t.tools || []).length}</td>
                  <td className="muted text-sm" style={{ maxWidth: 360, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {t.tagline || ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <TS_NewToolsetModal
          onClose={() => setCreateOpen(false)}
          onCreate={(row) => {
            setCreateOpen(false);
            if (typeof pushToast === "function") {
              pushToast({ kind: "success", title: "Toolset created", detail: row.id });
            }
            catalogue.refetch();
            navigate("/toolsets/" + row.id);
          }}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}

function _tsTransport(t) {
  return t?.config?.transport || null;
}

function _tsTarget(t) {
  const cfg = t?.config?.config || {};
  if (cfg.url) return cfg.url;
  if (Array.isArray(cfg.command) && cfg.command.length > 0) return cfg.command.join(" ");
  if (typeof cfg.command === "string") return cfg.command;
  return null;
}

// ============================================================================
// New toolset modal — MCP-stdio + MCP-http variants
// ============================================================================

function TS_NewToolsetModal({ onClose, onCreate, pushToast, existing }) {
  // Same modal: create when existing == null, otherwise edit. In edit
  // mode the id field locks and PUT-replaces.
  const isEdit = !!existing;
  const { useMutation, apiFetch } = window.primerApi;

  // Re-hydrate dict[str,str] fields back into the KV-editor pair shape.
  const _dictToPairs = (d) => d && typeof d === "object"
    ? Object.entries(d).map(([key, value]) => ({ key, value: String(value) }))
    : [];
  const _initialCommand = () => {
    const cmd = existing?.config?.config?.command;
    return Array.isArray(cmd) ? cmd.join(" ") : "";
  };
  const _initialTransport = () =>
    existing?.config?.transport === "http" ? "http" : "stdio";

  const [id, setId] = React.useState(existing?.id || "");
  const [provider, setProvider] = React.useState(existing?.provider || "mcp");
  const [transport, setTransport] = React.useState(_initialTransport);
  const [command, setCommand] = React.useState(_initialCommand);
  const [stdioEnv, setStdioEnv] = React.useState(
    () => _dictToPairs(existing?.config?.config?.env)
  );
  const [url, setUrl] = React.useState(existing?.config?.config?.url || "");
  const [httpHeaders, setHttpHeaders] = React.useState(
    () => _dictToPairs(existing?.config?.config?.headers)
  );
  const [fieldErrors, setFieldErrors] = React.useState({});

  const create = useMutation(
    (body) => isEdit
      ? apiFetch("PUT", "/toolsets/" + encodeURIComponent(existing.id), body)
      : apiFetch("POST", "/toolsets", body),
    {
      invalidates: isEdit
        ? ["toolsets:list", "toolset-detail:" + (existing?.id || "")]
        : ["toolsets:list"],
      onSuccess: (row) => onCreate(row),
      onError: (err) => {
        if (err && err.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) {
            next[(fe.loc || []).join(".")] = fe.msg;
          }
          setFieldErrors(next);
        } else if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err?.title || (isEdit ? "Save failed" : "Create failed"),
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    }
  );

  const kvToDict = (pairs) => Object.fromEntries(
    pairs.map((p) => [p.key.trim(), p.value]).filter(([k]) => k.length > 0)
  );

  const submit = async () => {
    setFieldErrors({});
    let config = null;
    if (provider === "mcp") {
      config = transport === "stdio"
        ? {
            transport: "stdio",
            config: {
              command: command.trim().split(/\s+/).filter(Boolean),
              env: kvToDict(stdioEnv),
            },
          }
        : {
            transport: "http",
            config: {
              url: url.trim(),
              headers: kvToDict(httpHeaders),
            },
          };
    }
    const body = {
      ...(isEdit ? { id: existing.id } : (id ? { id } : {})),
      provider,
      ...(config ? { config } : {}),
    };
    try { await create.mutate(body); } catch (_e) { /* surfaced via onError */ }
  };

  const canSubmit = provider === "mcp"
    ? (transport === "stdio" ? !!command.trim() : !!url.trim())
    : false;

  return (
    <Modal
      title={isEdit ? `Edit toolset · ${existing.id}` : "New toolset"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "check" : "plus"} onClick={submit} disabled={!canSubmit || create.loading}>
            {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create")}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">ID {isEdit
          ? <span className="hint">locked — id cannot change after create</span>
          : <span className="hint">optional — backend assigns if blank</span>}
        </label>
        <input
          className="input"
          value={id}
          onChange={(e) => setId(e.target.value)}
          placeholder="auto-generated"
          disabled={isEdit}
          style={{ width: "100%" }}
        />
        {fieldErrors["body.id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.id"]}</div>}
      </div>

      <div className="field">
        <label className="field-label">Provider</label>
        <select
          className="select"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="mcp">MCP server</option>
        </select>
        <div className="field-help">
          Internal toolsets (<span className="mono">system</span>, <span className="mono">workspaces</span>, <span className="mono">misc</span>, <span className="mono">search</span>, <span className="mono">web</span>) are runtime built-ins — they cannot be created via this form.
        </div>
        {fieldErrors["body.provider"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.provider"]}</div>}
      </div>

      {provider === "mcp" && (
        <>
          <div className="field">
            <label className="field-label">Transport</label>
            <div className="chip-group" style={{ display: "inline-flex" }}>
              <span
                className={`chip ${transport === "stdio" ? "active" : ""}`}
                onClick={() => setTransport("stdio")}
              >stdio</span>
              <span
                className={`chip ${transport === "http" ? "active" : ""}`}
                onClick={() => setTransport("http")}
              >http</span>
            </div>
            <div className="field-help">
              Per app spec, MCP TransportType only enumerates stdio + http (no sse).
            </div>
          </div>

          {transport === "stdio" ? (
            <>
              <div className="field">
                <label className="field-label">Command</label>
                <input
                  className="input mono"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="npx @modelcontextprotocol/server-github"
                  style={{ width: "100%" }}
                />
                {/*
                  T0245 / U0014 — anomaly-surface warning. The allowlist is
                  server-side (AppConfig.mcp_stdio_allowed_commands) and not
                  reachable from the UI, so we always render the warning when
                  transport=stdio is selected. First session-open will raise
                  ConfigError if the first token is not in the allowlist.
                */}
                <div className="field-help warn">
                  Space-separated argv. First token must be in <span className="mono">AppConfig.mcp_stdio_allowed_commands</span> or the first session-open call will raise ConfigError.
                </div>
                {fieldErrors["body.config.config.command"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.config.config.command"]}</div>}
              </div>
              <TS_KvEditor
                label="Environment"
                hint="optional · env vars set when launching the subprocess"
                pairs={stdioEnv}
                onChange={setStdioEnv}
                keyPlaceholder="GITHUB_TOKEN"
                valuePlaceholder="ghp_…"
              />
            </>
          ) : (
            <>
              <div className="field">
                <label className="field-label">URL</label>
                <input
                  className="input mono"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://mcp.example.com/sse"
                  style={{ width: "100%" }}
                />
                {fieldErrors["body.config.config.url"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.config.config.url"]}</div>}
              </div>
              <TS_KvEditor
                label="Headers"
                hint="optional · sent on every request to the MCP server"
                pairs={httpHeaders}
                onChange={setHttpHeaders}
                keyPlaceholder="Authorization"
                valuePlaceholder="Bearer …"
              />
            </>
          )}
        </>
      )}
    </Modal>
  );
}

// Reusable key/value editor for dict[str, str] fields (env vars, HTTP headers).
function TS_KvEditor({ label, hint, pairs, onChange, keyPlaceholder, valuePlaceholder }) {
  const updateAt = (i, patch) => onChange(pairs.map((p, idx) => idx === i ? { ...p, ...patch } : p));
  const removeAt = (i) => onChange(pairs.filter((_, idx) => idx !== i));
  const add = () => onChange([...pairs, { key: "", value: "" }]);
  return (
    <div className="field">
      <label className="field-label">{label} {hint && <span className="hint">{hint}</span>}</label>
      {pairs.length === 0 && <div className="field-help muted">— none —</div>}
      {pairs.map((p, i) => (
        <div key={i} style={{ display: "flex", gap: 6, marginTop: 4 }}>
          <input
            className="input mono"
            value={p.key}
            onChange={(e) => updateAt(i, { key: e.target.value })}
            placeholder={keyPlaceholder}
            style={{ flex: 1 }}
          />
          <input
            className="input mono"
            value={p.value}
            onChange={(e) => updateAt(i, { value: e.target.value })}
            placeholder={valuePlaceholder}
            style={{ flex: 2 }}
          />
          <Btn size="sm" kind="ghost" onClick={() => removeAt(i)} title="Remove">×</Btn>
        </div>
      ))}
      <div style={{ marginTop: 6 }}>
        <Btn size="sm" kind="ghost" icon="plus" onClick={add}>Add {label.toLowerCase().replace(/s$/, "")}</Btn>
      </div>
    </div>
  );
}

// ============================================================================
// Toolset detail (tabbed: Config / Tools / Sessions)
// ============================================================================

function ToolsetDetail({ toolsetId, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { params, query, navigate } = useRouter();
  const id = toolsetId || params.id;
  const tab = TS_TABS.some((t) => t.id === query.tab) ? query.tab : "config";
  const setTab = (t) => navigate("/toolsets/" + id, { tab: t });

  const detail = useResource(
    "toolset-detail:" + id,
    (signal) => apiFetch("GET", "/toolsets/" + encodeURIComponent(id), null, { signal }),
    { pollMs: null, deps: [id] }
  );

  const invalidate = useMutation(
    () => apiFetch("POST", "/toolsets/" + encodeURIComponent(id) + "/invalidate"),
    {
      invalidates: ["toolset-tools:" + id, "toolset-detail:" + id],
      onSuccess: () => {
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Cache dropped", detail: id });
        }
      },
      onError: _tsToastErr(pushToast, "Invalidate failed"),
    }
  );

  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [cascadeError, setCascadeError] = React.useState(null);

  const del = useMutation(
    () => apiFetch("DELETE", "/toolsets/" + encodeURIComponent(id)),
    {
      invalidates: ["toolsets:list"],
      onSuccess: () => {
        if (typeof pushToast === "function") {
          pushToast({ kind: "warning", title: "Toolset deleted", detail: id });
        }
        navigate("/toolsets");
      },
      onError: (err) => {
        if (err && err.status === 409) {
          // Cascade conflict — surface inline in the delete modal so the
          // operator can see which ToolApprovalPolicy still references this
          // toolset before the delete clears.
          setCascadeError(err);
        } else if (typeof pushToast === "function") {
          pushToast({
            kind: "error",
            title: err?.title || "Delete failed",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    }
  );

  if (detail.loading && !detail.data) {
    return (
      <div className="col" style={{ gap: 14 }}>
        <TS_DetailActions onInvalidate={null} onDelete={null} onBack={() => navigate("/toolsets")} />
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      </div>
    );
  }
  if (detail.error && !detail.data) {
    return (
      <div className="col" style={{ gap: 14 }}>
        <TS_DetailActions onInvalidate={null} onDelete={null} onBack={() => navigate("/toolsets")} />
        <Banner
          kind="error"
          title={detail.error.title || "Couldn't load toolset"}
          detail={detail.error.detail || detail.error.message}
          actions={<Btn size="sm" icon="chevron-left" onClick={() => navigate("/toolsets")}>Back to list</Btn>}
        />
      </div>
    );
  }

  const ts = detail.data;

  return (
    <div className="col" style={{ gap: 14 }}>
      <TS_DetailActions
        onInvalidate={() => invalidate.mutate()}
        onDelete={() => { setCascadeError(null); setConfirmDelete(true); }}
        onBack={() => navigate("/toolsets")}
      />

      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {TS_TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => setTab(t.id)}
              className={tab === t.id ? "active" : ""}
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
              <Icon name={t.icon} size={13} />
              {t.label}
            </button>
          ))}
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {tab === "config" && <TS_ConfigTab ts={ts} pushToast={pushToast} />}
          {tab === "tools" && <TS_ToolsTab id={id} ts={ts} onInvalidate={() => invalidate.mutate()} />}
          {tab === "sessions" && <TS_SessionsTab id={id} />}
        </div>
      </div>

      {confirmDelete && (
        <Modal
          title={`Delete ${id}?`}
          danger
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setConfirmDelete(false)}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                disabled={del.loading}
                onClick={async () => {
                  try { await del.mutate(); setConfirmDelete(false); }
                  catch (_e) { /* onError handled inline */ }
                }}
              >Delete</Btn>
            </>
          }
        >
          {cascadeError && (
            <Banner
              kind="error"
              title={cascadeError.title || "Delete blocked"}
              detail={cascadeError.detail || cascadeError.message || "Cascade conflict — a ToolApprovalPolicy still references this toolset. Disable or delete it first."}
            />
          )}
          <ul>
            <li>Removes the toolset row from storage.</li>
            <li>Any agent referencing this toolset will fail at next session-open with a config error.</li>
            <li>A ToolApprovalPolicy referencing this toolset will block the delete with 409 — resolve it first.</li>
            <li>DELETE is NOT idempotent on entities — a second DELETE returns 404 (app spec §5).</li>
          </ul>
        </Modal>
      )}
    </div>
  );
}

// Internal action bar — rendered INSIDE the page body so the outer
// page-header (rendered by app.jsx) is supplemented by detail-level
// actions without double-rendering the title.
function TS_DetailActions({ onInvalidate, onDelete, onBack }) {
  return (
    <div className="page-header" style={{ marginBottom: 0, justifyContent: "flex-end" }}>
      <div className="page-actions">
        {onInvalidate && <Btn icon="refresh" kind="ghost" onClick={onInvalidate}>Invalidate</Btn>}
        {onDelete && <Btn icon="trash" kind="danger" onClick={onDelete}>Delete</Btn>}
        {onBack && <Btn icon="chevron-left" kind="ghost" onClick={onBack}>Back</Btn>}
      </div>
    </div>
  );
}

// ============================================================================
// Config tab — read-only JSON dump
// ============================================================================

function TS_ConfigTab({ ts, pushToast }) {
  const hl = window.primerVendor?.highlightJson;
  const transport = _tsTransport(ts);
  const isManaged = !!ts?.harness_id;
  const pretty = React.useMemo(() => JSON.stringify(ts, null, 2), [ts]);
  const [editing, setEditing] = React.useState(false);

  return (
    <div style={{ padding: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10, gap: 10 }}>
        <div className="muted text-sm">
          {isManaged ? (
            <>Managed by harness <span className="mono">{ts.harness_id}</span>. Update the harness instead.</>
          ) : (
            <>
              PUT-replace edit via the form. Provider <span className="mono">{ts?.provider || "—"}</span>
              {transport && <> · transport <span className="mono">{transport}</span></>}
            </>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
          {!isManaged && (
            <Btn size="sm" icon="edit" kind="secondary" onClick={() => setEditing(true)}>Edit</Btn>
          )}
        </div>
      </div>
      {editing && (
        <TS_NewToolsetModal
          existing={ts}
          pushToast={pushToast}
          onClose={() => setEditing(false)}
          onCreate={() => {
            setEditing(false);
            if (typeof pushToast === "function") {
              pushToast({ kind: "info", title: "Toolset updated", detail: ts.id });
            }
          }}
        />
      )}
      {hl
        ? <div className="code-block" dangerouslySetInnerHTML={{ __html: hl(pretty) }} />
        : <pre className="code-block">{pretty}</pre>}
    </div>
  );
}

// ============================================================================
// Tools tab — fetches /tools + T0711 banner + per-tool approval badges
// ============================================================================

function TS_ToolsTab({ id, ts, onInvalidate }) {
  const { useResource, apiFetch } = window.primerApi;
  const tools = useResource(
    "toolset-tools:" + id,
    (signal) => apiFetch("GET", "/toolsets/" + encodeURIComponent(id) + "/tools", null, { signal }),
    { pollMs: null, deps: [id] }
  );
  // §12.4 of tool-approval spec: per-tool approval badge. Fetch all
  // policies once and look up by (toolset_id, tool_name).
  const policies = useResource(
    "toolsets:approval-policies",
    (signal) => apiFetch("GET", "/tool_approval_policies?limit=500", null, { signal }),
    { pollMs: null, deps: [] }
  );

  if (tools.loading && !tools.data) {
    return <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>Loading tools…</div>;
  }
  if (tools.error) {
    // T0711 — GET /v1/toolsets/{id}/tools leaks 500/errors/internal for
    // the MCP-HTTP transport when the remote server is unreachable.
    const transport = _tsTransport(ts);
    const t711 = tools.error.status === 500 && transport === "http";
    if (t711) {
      return (
        <div style={{ padding: 14 }}>
          <Banner
            kind="error"
            title="Tools list unavailable"
            detail={
              "This is the documented bug pinned by T0711 — the MCP-HTTP " +
              "transport leaks 500 /errors/internal when the remote server " +
              "is unreachable. Confirm the URL is reachable, then Invalidate " +
              "to drop the cached provider and retry."
            }
            actions={
              <>
                <Btn size="sm" icon="refresh" onClick={tools.refetch}>Retry</Btn>
                {onInvalidate && <Btn size="sm" kind="ghost" onClick={onInvalidate}>Invalidate</Btn>}
              </>
            }
          />
        </div>
      );
    }
    return (
      <div style={{ padding: 14 }}>
        <Banner
          kind="error"
          title={tools.error.title || "Couldn't load tools"}
          detail={tools.error.detail || tools.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={tools.refetch}>Retry</Btn>}
        />
      </div>
    );
  }
  const items = tools.data?.tools ?? [];
  if (items.length === 0) {
    return <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>No tools exposed by this toolset.</div>;
  }
  const policyItems = policies.data?.items ?? [];
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        {items.length} tool{items.length === 1 ? "" : "s"}. Canonical identifier is <span className="mono">id</span> (T0140/T0141), not <span className="mono">name</span>.
      </div>
      <table className="tbl">
        <thead>
          <tr>
            <th>name</th>
            <th>description</th>
            <th>approval</th>
          </tr>
        </thead>
        <tbody>
          {items.map((tool, i) => {
            const toolName = tool.name || tool.id;
            const policy = policyItems.find(
              (p) => p.toolset_id === id && p.tool_name === toolName && p.enabled,
            );
            return (
              <tr key={tool.id || toolName || i}>
                <td className="mono">{tool.id || toolName}</td>
                <td className="muted text-sm" style={{ fontSize: 11 }}>{tool.description || tool.desc || "—"}</td>
                <td>
                  {policy ? (
                    <span
                      className="pill"
                      style={{
                        background: "var(--bg-2)",
                        color: "var(--amber)",
                        border: "1px solid var(--border)",
                      }}
                      title={`Policy ${policy.id}`}
                    >
                      <span className="dot" style={{ background: "var(--amber)" }}></span>
                      ⓘ approval: {policy.approval?.type || "required"}
                    </span>
                  ) : (
                    <span className="muted text-sm">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ============================================================================
// Sessions tab — joins broad sessions list with agents that reference this id
// ============================================================================

function TS_SessionsTab({ id }) {
  const { useResource, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const sessions = useResource(
    "toolset-sessions:" + id,
    (signal) => apiFetch("GET", "/sessions?limit=200", null, { signal }),
    { pollMs: null, deps: [id] }
  );
  const agents = useResource(
    "toolset-agents-for-filter:" + id,
    (signal) => apiFetch("GET", "/agents?limit=200", null, { signal }),
    { pollMs: null, deps: [id] }
  );

  if ((sessions.loading && !sessions.data) || (agents.loading && !agents.data)) {
    return <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>Loading…</div>;
  }
  if (sessions.error && !sessions.data) {
    return (
      <div style={{ padding: 14 }}>
        <Banner
          kind="error"
          title={sessions.error.title || "Couldn't load sessions"}
          detail={sessions.error.detail || sessions.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={sessions.refetch}>Retry</Btn>}
        />
      </div>
    );
  }

  const agentsUsing = new Set(
    (agents.data?.items ?? [])
      .filter((a) => Array.isArray(a.tools) && a.tools.includes(id))
      .map((a) => a.id)
  );
  const matched = (sessions.data?.items ?? []).filter(
    (s) => s.binding?.agent_id && agentsUsing.has(s.binding.agent_id)
  );

  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        Sessions whose bound agent references <span className="mono">{id}</span> ({agentsUsing.size} agent{agentsUsing.size === 1 ? "" : "s"}).
      </div>
      {matched.length === 0 ? (
        <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>No sessions reference this toolset.</div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr><th>Status</th><th>Session</th><th>Agent</th><th>Workspace</th></tr>
            </thead>
            <tbody>
              {matched.map((s) => (
                <tr key={s.id} onClick={() => navigate("/sessions/" + s.id)} style={{ cursor: "pointer" }}>
                  <td><span className={`pill pill-${_tsPillCls(s.status)}`}><span className="dot"></span>{s.status}</span></td>
                  <td className="mono">{s.id}</td>
                  <td className="mono">{s.binding?.agent_id || "—"}</td>
                  <td className="mono muted">{s.workspace_id || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function _tsPillCls(status) {
  if (status === "running") return "running";
  if (status === "paused") return "paused";
  if (status === "ended" || status === "completed") return "ended";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "cancelled";
  if (status === "claimed") return "claimed";
  return "";
}

// ============================================================================
// Tools page — every tool, with editable approval config per row
// ============================================================================
//
// Flat table over /v1/tools (which fans out every toolset's tools into one
// flat list with `builtin: true|false`). Joined with /v1/tool_approval_policies
// so each row shows its current policy (or "—") and an Edit/Add button that
// opens the AP_NewPolicyModal pre-seeded with the row's (toolset_id, tool_id).
//
// Approval policy edits apply to every tool — built-in and user — so an
// operator can require human approval on `system.fs_delete` the same way they
// can on a custom MCP tool.

function ToolsPage({ pushToast }) {
  const { useResource, apiFetch } = window.primerApi;
  const catalogue = useResource(
    "tools:catalogue",
    (signal) => apiFetch("GET", "/tools", null, { signal }),
    { pollMs: null }
  );
  const policies = useResource(
    "tools:policies",
    (signal) => apiFetch("GET", "/tool_approval_policies?limit=200", null, { signal }),
    { pollMs: null }
  );

  const [textFilter, setTextFilter] = React.useState("");
  const [policyFilter, setPolicyFilter] = React.useState("");
  const [editing, setEditing] = React.useState(null); // {tool_id, toolset_id, builtin, policy?}

  // (toolset_id, tool_name) → policy row.
  const policyIndex = React.useMemo(() => {
    const ix = {};
    for (const p of policies.data?.items ?? []) {
      ix[`${p.toolset_id}::${p.tool_name}`] = p;
    }
    return ix;
  }, [policies.data]);

  // Flatten the catalogue into one row per tool.
  const rows = React.useMemo(() => {
    const out = [];
    for (const ts of catalogue.data?.items ?? []) {
      for (const t of ts.tools || []) {
        out.push({
          tool_id: t.id,
          scoped_id: t.scoped_id || `${ts.id}__${t.id}`,
          toolset_id: ts.id,
          builtin: !!ts.builtin,
          available: !!ts.available,
          description: t.description || "",
          policy: policyIndex[`${ts.id}::${t.id}`] || null,
        });
      }
    }
    return out;
  }, [catalogue.data, policyIndex]);

  const filtered = React.useMemo(() => {
    let arr = rows;
    if (textFilter) {
      const q = textFilter.toLowerCase();
      arr = arr.filter((r) =>
        r.tool_id.toLowerCase().includes(q)
        || r.toolset_id.toLowerCase().includes(q)
        || r.description.toLowerCase().includes(q));
    }
    if (policyFilter === "with") arr = arr.filter((r) => r.policy);
    if (policyFilter === "without") arr = arr.filter((r) => !r.policy);
    return arr;
  }, [rows, textFilter, policyFilter]);

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter tools…"
            value={textFilter}
            onChange={(e) => setTextFilter(e.target.value)}
          />
        </div>
        <div className="sep-v" />
        <select
          className="select"
          value={policyFilter}
          onChange={(e) => setPolicyFilter(e.target.value)}
        >
          <option value="">all tools</option>
          <option value="with">with approval policy</option>
          <option value="without">without approval policy</option>
        </select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={() => {
            catalogue.refetch();
            policies.refetch();
          }}>Refresh</Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>Tool</th>
              <th>Toolset</th>
              <th>Kind</th>
              <th>Approval</th>
              <th>Description</th>
              <th style={{ width: 100, textAlign: "right" }}></th>
            </tr>
          </thead>
          <tbody>
            {catalogue.loading && rows.length === 0 ? (
              <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</td></tr>
            ) : catalogue.error && rows.length === 0 ? (
              <tr><td colSpan={6} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{catalogue.error.title || catalogue.error.message}</span>
                {" · "}<a onClick={catalogue.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No tools match.</td></tr>
            ) : filtered.map((r) => {
              const type = r.policy?.approval?.type;
              const typeColor = type === "required" ? "var(--amber)"
                : type === "policy" ? "var(--blue)"
                : type === "llm" ? "var(--violet)"
                : "var(--text-4)";
              return (
                <tr key={r.scoped_id}>
                  <td className="mono">{r.tool_id}</td>
                  <td className="mono muted text-sm">{r.toolset_id}</td>
                  <td>
                    <span className="pill" style={{
                      color: r.builtin ? "var(--blue)" : "var(--accent)",
                      borderColor: "var(--border)", background: "var(--bg-2)",
                    }}>
                      <span className="dot" style={{ background: r.builtin ? "var(--blue)" : "var(--accent)" }}></span>
                      {r.builtin ? "built-in" : "user"}
                    </span>
                  </td>
                  <td>
                    {r.policy ? (
                      <span className="pill" style={{ color: typeColor, borderColor: "var(--border)", background: "var(--bg-2)" }}>
                        <span className="dot" style={{ background: typeColor }}></span>
                        {type}{!r.policy.enabled && " · off"}
                      </span>
                    ) : (
                      <span className="muted text-sm">—</span>
                    )}
                  </td>
                  <td className="muted text-sm" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {r.description}
                  </td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}>
                    <Btn size="sm" kind={r.policy ? "ghost" : "secondary"} icon={r.policy ? "edit" : "plus"} onClick={() => setEditing(r)}>
                      {r.policy ? "Edit" : "Add"}
                    </Btn>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {editing && (
        <AP_NewPolicyModal
          existing={editing.policy || _seedPolicy(editing)}
          pushToast={pushToast}
          onClose={() => {
            setEditing(null);
            policies.refetch();
          }}
        />
      )}
    </div>
  );
}

// Construct a partial policy row from a tool entry — used to pre-seed
// AP_NewPolicyModal in "Add" mode so the toolset_id and tool_name are
// already filled. Modal is still in create-mode (no `existing` row yet);
// we just inject defaults via the `existing` prop.
function _seedPolicy(toolRow) {
  return {
    // No `id` — modal will treat this as create (id field stays editable).
    id: "",
    toolset_id: toolRow.toolset_id,
    tool_name: toolRow.tool_id,
    enabled: true,
    approval: { type: "required" },
  };
}

window.ToolsetsPage = ToolsetsPage;
window.ToolsPage = ToolsPage;
window.ToolsetDetail = ToolsetDetail;
