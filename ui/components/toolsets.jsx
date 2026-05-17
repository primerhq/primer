/* global React, Icon, Btn, Modal, Banner, relativeTime */

const { apiFetch, useResource, useMutation, useRouter, useToast } = window.matrixApi;

const BUILTIN_IDS = ["_system", "_workspaces", "_search", "web"];

// ============================================================================
// Page dispatcher (kind = "user" | "builtin")
// ============================================================================

function ToolsetsPage({ kind }) {
  return kind === "builtin" ? <BuiltinToolsetsPage /> : <UserToolsetsPage />;
}

// ============================================================================
// User toolsets
// ============================================================================

function UserToolsetsPage() {
  const { navigate } = useRouter();
  const { push: pushToast } = useToast();

  const list = useResource("toolsets:list",
    (s) => apiFetch("GET", "/toolsets?limit=200", null, { signal: s }), {});
  const [createOpen, setCreateOpen] = React.useState(false);
  const [textFilter, setTextFilter] = React.useState("");

  // Filter out built-in ids — they're surfaced on a separate page.
  const items = (list.data?.items ?? []).filter((t) => !BUILTIN_IDS.includes(t.id) && !t.id.startsWith("_"));
  const filtered = items.filter((t) => !textFilter || t.id.toLowerCase().includes(textFilter.toLowerCase()));

  return (
    <div className="col" style={{ gap: 14 }}>
      <UserToolsetsHeader count={items.length} onRefresh={list.refetch} onNew={() => setCreateOpen(true)} />

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter toolsets…" value={textFilter} onChange={(e) => setTextFilter(e.target.value)} />
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New toolset</Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Provider</th>
              <th>Transport</th>
              <th>Target</th>
            </tr>
          </thead>
          <tbody>
            {list.loading && items.length === 0 ? (
              <tr><td colSpan={4} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</td></tr>
            ) : list.error && items.length === 0 ? (
              <tr><td colSpan={4} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{list.error.title || list.error.message}</span>
                {" · "}<a onClick={list.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              items.length === 0 ? (
                <tr><td colSpan={4}>
                  <div className="empty" style={{ padding: "40px 20px" }}>
                    <div className="ico-wrap"><Icon name="tools" size={22} /></div>
                    <div className="head">No user toolsets yet</div>
                    <div className="sub">User toolsets are MCP servers (stdio or http). Built-in toolsets (_system, _workspaces, _search, web) live on a separate page.</div>
                    <div className="actions"><Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New toolset</Btn></div>
                  </div>
                </td></tr>
              ) : (
                <tr><td colSpan={4} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No toolsets match.</td></tr>
              )
            ) : filtered.map((t) => (
              <tr key={t.id} onClick={() => navigate("/toolsets/" + t.id)} style={{ cursor: "pointer" }}>
                <td className="mono">{t.id}</td>
                <td className="mono muted text-sm">{t.provider || "—"}</td>
                <td className="mono muted text-sm">{t.config?.transport || "—"}</td>
                <td className="mono muted text-sm">{t.config?.config?.command?.[0] || t.config?.config?.url || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <NewToolsetModal
          onClose={() => setCreateOpen(false)}
          onCreate={(t) => {
            setCreateOpen(false);
            pushToast({ kind: "success", title: "Toolset created", detail: t.id });
            list.refetch();
            navigate("/toolsets/" + t.id);
          }}
        />
      )}
    </div>
  );
}

function UserToolsetsHeader({ count, onRefresh, onNew }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span>Toolsets</span><span className="sep">/</span><span style={{ color: "var(--text)" }}>User</span>
        </div>
        <h1 className="page-title">User toolsets</h1>
        <div className="page-sub tabular">{count} toolset{count === 1 ? "" : "s"} · MCP servers (stdio/http)</div>
      </div>
      <div className="page-actions">
        <Btn icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>
        <Btn icon="plus" kind="primary" onClick={onNew}>New toolset</Btn>
      </div>
    </div>
  );
}

// ============================================================================
// User toolset create modal
// ============================================================================

// Provider-pattern: the toolset record has a top-level `provider` field
// (currently only "mcp" is creatable — "internal" toolsets are runtime
// built-ins and must not come from a row). Future providers (e.g. a
// future REST-tool provider) would slot in here.
const TOOLSET_PROVIDERS = [
  { value: "mcp", label: "MCP server" },
];

function NewToolsetModal({ onClose, onCreate }) {
  const { push: pushToast } = useToast();
  const [id, setId] = React.useState("");
  const [provider, setProvider] = React.useState("mcp");
  const [transport, setTransport] = React.useState("stdio");
  const [command, setCommand] = React.useState("");
  const [stdioEnv, setStdioEnv] = React.useState([]);   // [{key, value}, ...]
  const [url, setUrl] = React.useState("");
  const [httpHeaders, setHttpHeaders] = React.useState([]);  // [{key, value}, ...]
  const [fieldErrors, setFieldErrors] = React.useState({});

  const create = useMutation(
    (body) => apiFetch("POST", "/toolsets", body),
    {
      invalidates: ["toolsets:list"],
      onSuccess: (t) => onCreate(t),
      onError: (err) => {
        if (err.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) next[(fe.loc || []).join(".")] = fe.msg;
          setFieldErrors(next);
        } else {
          pushToast({ kind: "error", title: err.title || "Create failed", detail: err.detail || err.message, requestId: err.requestId });
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
      ...(id ? { id } : {}),
      provider,
      ...(config ? { config } : {}),
    };
    try { await create.mutate(body); } catch (_e) {}
  };

  const canSubmit = provider === "mcp"
    ? (transport === "stdio" ? !!command.trim() : !!url.trim())
    : false;

  return (
    <Modal
      title="New toolset"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit} disabled={!canSubmit || create.loading}>
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">ID <span className="hint">optional — backend assigns if blank</span></label>
        <input className="input" value={id} onChange={(e) => setId(e.target.value)} placeholder="auto-generated" style={{ width: "100%" }} />
      </div>
      <div className="field">
        <label className="field-label">Provider</label>
        <select className="select" value={provider} onChange={(e) => setProvider(e.target.value)} style={{ width: "100%" }}>
          {TOOLSET_PROVIDERS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
        </select>
        <div className="field-help">
          Internal toolsets (<span className="mono">_system</span>, <span className="mono">_workspaces</span>, <span className="mono">_misc</span>, <span className="mono">_search</span>, <span className="mono">web</span>) are runtime built-ins — they cannot be created via this form.
        </div>
        {fieldErrors["body.provider"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.provider"]}</div>}
      </div>

      {provider === "mcp" && (
        <>
          <div className="field">
            <label className="field-label">Transport</label>
            <div className="chip-group" style={{ display: "inline-flex" }}>
              <span className={`chip ${transport === "stdio" ? "active" : ""}`} onClick={() => setTransport("stdio")}>stdio</span>
              <span className={`chip ${transport === "http" ? "active" : ""}`} onClick={() => setTransport("http")}>http</span>
            </div>
            <div className="field-help">
              Per app spec, MCP TransportType only enumerates stdio + http (no sse).
            </div>
          </div>

          {transport === "stdio" ? (
            <>
              <div className="field">
                <label className="field-label">Command</label>
                <input className="input mono" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx @modelcontextprotocol/server-github" style={{ width: "100%" }} />
                <div className="field-help">
                  Space-separated argv. First token must be in <span className="mono">AppConfig.mcp_stdio_allowed_commands</span> or the first session-open will raise ConfigError.
                </div>
                {fieldErrors["body.config.config.command"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.config.config.command"]}</div>}
              </div>
              <KvEditor
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
                <input className="input mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://mcp.example.com/sse" style={{ width: "100%" }} />
                {fieldErrors["body.config.config.url"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.config.config.url"]}</div>}
              </div>
              <KvEditor
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

// Reusable key/value editor for dict[str, str] fields (env vars, HTTP headers, etc.)
function KvEditor({ label, hint, pairs, onChange, keyPlaceholder, valuePlaceholder }) {
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
// Toolset detail
// ============================================================================

function ToolsetDetail() {
  const { params, query: routerQuery, navigate } = useRouter();
  const { push: pushToast } = useToast();
  const id = params.id;
  const tab = ["config", "tools", "sessions"].includes(routerQuery.tab) ? routerQuery.tab : "config";

  const detail = useResource("toolset-detail:" + id,
    (s) => apiFetch("GET", "/toolsets/" + encodeURIComponent(id), null, { signal: s }),
    { pollMs: null, deps: [id] });

  const invalidate = useMutation(
    () => apiFetch("POST", "/toolsets/" + encodeURIComponent(id) + "/invalidate"),
    {
      invalidates: ["toolsets:list", `toolset-tools:${id}`],
      onSuccess: () => pushToast({ kind: "info", title: "Toolset cache dropped", detail: id }),
      onError: (err) => pushToast({ kind: "error", title: "Invalidate failed", detail: err.detail || err.message, requestId: err.requestId }),
    }
  );
  const delMut = useMutation(
    () => apiFetch("DELETE", "/toolsets/" + encodeURIComponent(id)),
    {
      invalidates: ["toolsets:list"],
      onSuccess: () => { pushToast({ kind: "warning", title: "Toolset deleted", detail: id }); navigate("/toolsets"); },
      onError: (err) => pushToast({ kind: "error", title: "Delete failed", detail: err.detail || err.message, requestId: err.requestId }),
    }
  );
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  if (detail.loading && !detail.data) {
    return (
      <>
        <ToolsetDetailHeader id={id} navigate={navigate} />
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      </>
    );
  }
  if (detail.error && !detail.data) {
    return (
      <>
        <ToolsetDetailHeader id={id} navigate={navigate} />
        <Banner kind="error" title={detail.error.title || "Couldn't load toolset"} detail={detail.error.detail || detail.error.message}
          actions={<Btn size="sm" icon="chevron-left" onClick={() => navigate("/toolsets")}>Back to list</Btn>} />
      </>
    );
  }
  const ts = detail.data;
  const setTab = (t) => navigate("/toolsets/" + id, { tab: t });

  return (
    <div className="col" style={{ gap: 14 }}>
      <ToolsetDetailHeader
        id={id}
        navigate={navigate}
        onInvalidate={() => invalidate.mutate()}
        onDelete={() => setConfirmDelete(true)}
      />
      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {[{ id: "config", label: "Config", icon: "settings" }, { id: "tools", label: "Tools", icon: "tools" }, { id: "sessions", label: "Sessions", icon: "zap" }].map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                background: "none", border: "none", padding: "10px 14px", cursor: "pointer",
                color: tab === t.id ? "var(--text)" : "var(--text-3)",
                fontSize: 12.5, fontWeight: tab === t.id ? 600 : 400,
                borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1, display: "inline-flex", alignItems: "center", gap: 6,
              }}
            >
              <Icon name={t.icon} size={13} />
              {t.label}
            </button>
          ))}
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {tab === "config" && <ToolsetConfigTab ts={ts} />}
          {tab === "tools" && <ToolsetToolsTab id={id} ts={ts} onInvalidate={() => invalidate.mutate()} />}
          {tab === "sessions" && <ToolsetSessionsTab id={id} />}
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
              <Btn kind="danger" icon="trash" onClick={async () => { setConfirmDelete(false); try { await delMut.mutate(); } catch (_e) {} }}>Delete</Btn>
            </>
          }
        >
          <ul>
            <li>Removes the toolset row from storage.</li>
            <li>Any agent referencing this toolset will fail at next session-open with a config error.</li>
            <li>DELETE is NOT idempotent on entities — a second DELETE returns 404 (app spec §5).</li>
          </ul>
        </Modal>
      )}
    </div>
  );
}

function ToolsetDetailHeader({ id, navigate, onInvalidate, onDelete }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="crumb">
          <a onClick={() => navigate("/toolsets")}>Toolsets</a>
          <span className="sep">/</span>
          <span className="mono" style={{ color: "var(--text)" }}>{id}</span>
        </div>
        <h1 className="page-title mono">{id}</h1>
      </div>
      <div className="page-actions">
        {onInvalidate && <Btn icon="refresh" kind="ghost" onClick={onInvalidate}>Invalidate</Btn>}
        {onDelete && <Btn icon="trash" kind="danger" onClick={onDelete}>Delete</Btn>}
        <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/toolsets")}>Back</Btn>
      </div>
    </div>
  );
}

function ToolsetConfigTab({ ts }) {
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">Read-only render. Edit a toolset via DELETE + POST; in-place PUT is not yet exposed.</div>
      <div className="code-block" dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(ts, null, 2)) }} />
    </div>
  );
}

function ToolsetToolsTab({ id, ts, onInvalidate }) {
  // Per app spec §8 / T0711: GET /v1/toolsets/{id}/tools can leak
  // 500 /errors/internal for the MCP-HTTP transport. We surface that
  // as a dedicated anomaly block so operators can act on it.
  const tools = useResource(`toolset-tools:${id}`,
    (s) => apiFetch("GET", "/toolsets/" + encodeURIComponent(id) + "/tools", null, { signal: s }),
    { pollMs: null, deps: [id] });

  if (tools.loading && !tools.data) {
    return <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>Loading tools…</div>;
  }
  if (tools.error) {
    const t711 = tools.error.status === 500 && ts?.config?.transport === "http";
    if (t711) {
      return (
        <div style={{ padding: 14 }}>
          <Banner
            kind="error"
            title="Tools list unavailable"
            detail={
              "This is the documented bug pinned by T0711 — the MCP-HTTP " +
              "transport leaks 500/errors/internal when the remote server is " +
              "unreachable. Confirm the URL is reachable, then Invalidate to " +
              "drop the cached provider and retry."
            }
            actions={
              <>
                <Btn size="sm" icon="refresh" onClick={tools.refetch}>Retry</Btn>
                <Btn size="sm" kind="ghost" onClick={onInvalidate}>Invalidate</Btn>
              </>
            }
          />
        </div>
      );
    }
    return (
      <div style={{ padding: 14 }}>
        <Banner kind="error" title={tools.error.title || "Couldn't load tools"} detail={tools.error.detail || tools.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={tools.refetch}>Retry</Btn>} />
      </div>
    );
  }
  const items = tools.data?.tools ?? [];
  if (items.length === 0) {
    return <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>No tools exposed by this toolset.</div>;
  }
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">{items.length} tool{items.length === 1 ? "" : "s"}. Canonical identifier is <span className="mono">id</span> (T0140/T0141), not <span className="mono">name</span>.</div>
      {items.map((tool, i) => (
        <ToolCard key={tool.id || i} tool={tool} />
      ))}
    </div>
  );
}

function ToolCard({ tool }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div className="panel" style={{ marginBottom: 10 }}>
      <div className="panel-h" onClick={() => setOpen(!open)} style={{ cursor: "pointer" }}>
        <Icon name={open ? "chevron-down" : "chevron-right"} size={11} className="muted" />
        <Icon name="tools" size={12} className="muted" />
        <span className="mono">{tool.id}</span>
        {tool.toolset_id && <span className="sub mono">· {tool.toolset_id}</span>}
        <div className="right">
          <Btn size="sm" kind="ghost" disabled title="Tool invocation endpoint not yet implemented (planned — backend-additions §2.2)">Test call</Btn>
        </div>
      </div>
      {open && (
        <div className="panel-body">
          {tool.description && <div className="muted text-sm mb-3">{tool.description}</div>}
          {tool.schema && (
            <div className="code-block" dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(tool.schema, null, 2)) }} />
          )}
        </div>
      )}
    </div>
  );
}

function ToolsetSessionsTab({ id }) {
  // No backend `?toolset_id=` filter; v1 fetches the broad sessions list
  // and joins client-side via the agent's `tools` field. Revisit if the
  // operator has a high session count.
  const sessions = useResource("toolsets-sessions",
    (s) => apiFetch("GET", "/sessions?limit=200", null, { signal: s }),
    { pollMs: null });
  const agents = useResource("toolsets-agents-for-filter",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }),
    { pollMs: null });
  const { navigate } = useRouter();

  if (sessions.loading || agents.loading) {
    return <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>Loading…</div>;
  }

  const agentsUsing = new Set((agents.data?.items ?? []).filter((a) => Array.isArray(a.tools) && a.tools.includes(id)).map((a) => a.id));
  const matched = (sessions.data?.items ?? []).filter((s) => s.binding?.agent_id && agentsUsing.has(s.binding.agent_id));

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
            <thead><tr><th>Status</th><th>Session</th><th>Agent</th><th>Workspace</th></tr></thead>
            <tbody>
              {matched.map((s) => (
                <tr key={s.id} onClick={() => navigate("/sessions/" + s.id)} style={{ cursor: "pointer" }}>
                  <td><span className={`pill pill-${_pillCls(s.status)}`}><span className="dot"></span>{s.status}</span></td>
                  <td className="mono">{s.id}</td>
                  <td className="mono">{s.binding?.agent_id || "—"}</td>
                  <td className="mono muted">{s.workspace_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function _pillCls(status) {
  if (status === "running") return "running";
  if (status === "paused") return "paused";
  if (status === "ended" || status === "completed") return "ended";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "cancelled";
  if (status === "claimed") return "claimed";
  return "";
}

// ============================================================================
// Built-in toolsets — read-only cards
// ============================================================================

function BuiltinToolsetsPage() {
  const ic = useResource("sidebar:ic-config",
    async (s) => { try { return await apiFetch("GET", "/internal_collections/config", null, { signal: s }); } catch (e) { if (e?.status === 404) return null; throw e; } },
    { pollMs: 30000 });
  const subsystemOn = ic.data != null;
  return (
    <div className="col" style={{ gap: 14 }}>
      <BuiltinHeader />
      <div className="muted text-sm">
        Built-in toolsets are runtime primitives; each card lists the tools currently exposed.
        Read-only; cannot be created or destroyed.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <BuiltinCard id="_system" tagline="Operator + diagnostic tools (always on)" icon="settings" available />
        <BuiltinCard id="_workspaces" tagline="File ops + exec inside the bound workspace (always on)" icon="box" available />
        <BuiltinCard id="_search" tagline={subsystemOn ? "Semantic search over indexed entities (subsystem ON)" : "Semantic search — unavailable while Internal Collections subsystem is OFF"} icon="search" available={subsystemOn} />
        <BuiltinCard id="web" tagline="DuckDuckGo search + page-fetch primitives (always on)" icon="external" available />
      </div>
    </div>
  );
}

function BuiltinHeader() {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span>Toolsets</span><span className="sep">/</span><span style={{ color: "var(--text)" }}>Built-in</span>
        </div>
        <h1 className="page-title">Built-in toolsets</h1>
        <div className="page-sub">Runtime-provided primitives — read-only</div>
      </div>
    </div>
  );
}

function BuiltinCard({ id, tagline, icon, available }) {
  const [open, setOpen] = React.useState(false);
  const tools = useResource(`toolset-tools:${id}`,
    (s) => open
      ? apiFetch("GET", "/toolsets/" + encodeURIComponent(id) + "/tools", null, { signal: s })
      : Promise.resolve(null),
    { pollMs: null, deps: [id, open] });
  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name={icon} size={13} className="muted" />
        <span className="mono">{id}</span>
        <span className={`pill ${available ? "pill-ended" : "pill-cancelled"}`} style={{ marginLeft: 8 }}>
          <span className="dot"></span>{available ? "available" : "unavailable"}
        </span>
      </div>
      <div className="panel-body">
        <div className="muted text-sm mb-3">{tagline}</div>
        {available && (
          <Btn size="sm" kind="ghost" onClick={() => setOpen(!open)}>{open ? "Hide tools" : "Show tools"}</Btn>
        )}
        {open && available && (
          <div className="mt-3">
            {tools.loading && !tools.data ? <div className="muted text-sm">Loading…</div>
              : tools.error ? <div style={{ color: "var(--red)", fontSize: 12 }}>{tools.error.title || tools.error.message}</div>
              : (tools.data?.tools ?? []).length === 0 ? <div className="muted text-sm">No tools exposed.</div>
              : (tools.data.tools.map((t) => (
                  <div key={t.id} style={{ padding: "4px 0", display: "flex", gap: 8, alignItems: "center", fontSize: 12, borderBottom: "1px solid var(--border)" }}>
                    <Icon name="tools" size={11} className="muted" />
                    <span className="mono">{t.id}</span>
                    {t.description && <span className="muted text-sm" style={{ marginLeft: "auto", textAlign: "right", maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.description}</span>}
                  </div>
                )))}
          </div>
        )}
      </div>
    </div>
  );
}

window.ToolsetsPage = ToolsetsPage;
window.ToolsetDetail = ToolsetDetail;
