/* global React, Icon, StatusPill, Btn, Modal, Banner, relativeTime */

const { apiFetch, useResource, useMutation, useRouter, useToast } = window.matrixApi;

const PROVIDER_COLORS = {
  openai: "var(--green)",
  anthropic: "var(--accent)",
  voyageai: "var(--blue)",
  cohere: "var(--violet)",
  ollama: "var(--amber)",
  google: "var(--blue)",
  gemini: "var(--blue)",
  huggingface: "var(--amber)",
  openresponses: "var(--green)",
};

// ============================================================================
// Agents list
// ============================================================================

function AgentsPage({ onNewSession }) {
  const { query: routerQuery, navigate } = useRouter();
  const { push: pushToast } = useToast();

  // Open create modal when navigated with ?create=1.
  const [createOpen, setCreateOpen] = React.useState(false);
  React.useEffect(() => {
    if (routerQuery.create === "1") {
      setCreateOpen(true);
      const rest = { ...routerQuery };
      delete rest.create;
      navigate("/agents", rest);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const list = useResource("agents:list",
    (s) => apiFetch("GET", "/agents?limit=200", null, { signal: s }), {});
  const providers = useResource("agents:llm-providers",
    (s) => apiFetch("GET", "/llm_providers?limit=200", null, { signal: s }), {});

  const [textFilter, setTextFilter] = React.useState("");
  const items = list.data?.items ?? [];
  const filtered = items.filter((a) =>
    !textFilter ||
    a.id.toLowerCase().includes(textFilter.toLowerCase()) ||
    (a.description || "").toLowerCase().includes(textFilter.toLowerCase())
  );

  // Per-row status — fetch /v1/agents/{id}/status for each visible row;
  // batched. No poll on the list page.
  const [perRowStatus, setPerRowStatus] = React.useState({});
  React.useEffect(() => {
    if (items.length === 0) return undefined;
    const ctrl = new AbortController();
    Promise.all(
      items.map((a) =>
        apiFetch("GET", `/agents/${encodeURIComponent(a.id)}/status`, null, { signal: ctrl.signal })
          .then((r) => [a.id, r])
          .catch((e) => [a.id, { ok: null, error: e.title || e.message }])
      )
    ).then((entries) => setPerRowStatus(Object.fromEntries(entries)));
    return () => ctrl.abort();
  }, [list.data]);

  // Per-row session count.
  const [perRowSessions, setPerRowSessions] = React.useState({});
  React.useEffect(() => {
    if (items.length === 0) return undefined;
    const ctrl = new AbortController();
    Promise.all(
      items.map((a) =>
        apiFetch("GET", `/sessions?agent_id=${encodeURIComponent(a.id)}&limit=1`, null, { signal: ctrl.signal })
          .then((r) => [a.id, r.total ?? 0])
          .catch(() => [a.id, null])
      )
    ).then((entries) => setPerRowSessions(Object.fromEntries(entries)));
    return () => ctrl.abort();
  }, [list.data]);

  return (
    <div className="col" style={{ gap: 14 }}>
      <AgentsHeader count={items.length} onRefresh={list.refetch} onNew={() => setCreateOpen(true)} />

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter agents…" value={textFilter} onChange={(e) => setTextFilter(e.target.value)} />
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New agent</Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Description</th>
              <th>Provider · model</th>
              <th>Tools</th>
              <th style={{ textAlign: "right" }}>Sessions</th>
              <th style={{ width: 100 }}>Status</th>
            </tr>
          </thead>
          <tbody>
            {list.loading && items.length === 0 ? (
              <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</td></tr>
            ) : list.error && items.length === 0 ? (
              <tr><td colSpan={6} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{list.error.title || list.error.message}</span>
                {" · "}<a onClick={list.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              items.length === 0 ? (
                <tr><td colSpan={6}>
                  <div className="empty" style={{ padding: "40px 20px" }}>
                    <div className="ico-wrap"><Icon name="agent" size={22} /></div>
                    <div className="head">No agents yet</div>
                    <div className="sub">Agents pair an LLM provider with a system prompt and a list of toolsets, then run inside a session.</div>
                    <div className="actions"><Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New agent</Btn></div>
                  </div>
                </td></tr>
              ) : (
                <tr><td colSpan={6} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No agents match.</td></tr>
              )
            ) : filtered.map((a) => {
              const providerId = a.model?.provider_id;
              const modelName = a.model?.model_name;
              const provider = (providers.data?.items ?? []).find((p) => p.id === providerId);
              const vendorColor = PROVIDER_COLORS[provider?.provider] || "var(--text-3)";
              const status = perRowStatus[a.id];
              const sessionCount = perRowSessions[a.id];
              return (
                <tr key={a.id} onClick={() => navigate("/agents/" + a.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{a.id}</td>
                  <td className="muted text-sm" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {a.description || <span style={{ color: "var(--text-4)" }}>—</span>}
                  </td>
                  <td className="mono text-sm">
                    {providerId
                      ? <>
                          <span className="dot" style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: vendorColor, marginRight: 6 }}></span>
                          {providerId}{modelName ? <span className="muted"> · {modelName}</span> : null}
                        </>
                      : <span className="muted">(unconfigured)</span>}
                  </td>
                  <td className="mono muted text-sm">{(a.tools ?? []).length}</td>
                  <td className="mono num tabular">
                    {sessionCount == null
                      ? <span className="muted">…</span>
                      : sessionCount > 0
                        ? <span style={{ color: "var(--blue)" }}>{sessionCount}</span>
                        : <span className="muted">0</span>}
                  </td>
                  <td>
                    {status == null ? (
                      <span className="muted">…</span>
                    ) : status.ok === true ? (
                      <span className="pill pill-ended"><span className="dot"></span>ok</span>
                    ) : status.ok === false ? (
                      <span className="pill pill-failed"><span className="dot"></span>{(status.issues || []).length} issue{(status.issues || []).length === 1 ? "" : "s"}</span>
                    ) : (
                      <span className="muted" title={status.error}>err</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <NewAgentModal
          onClose={() => setCreateOpen(false)}
          onCreate={(a) => {
            setCreateOpen(false);
            pushToast({ kind: "success", title: "Agent created", detail: a.id });
            list.refetch();
            navigate("/agents/" + a.id);
          }}
        />
      )}
    </div>
  );
}

function AgentsHeader({ count, onRefresh, onNew }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span>Compute</span><span className="sep">/</span><span style={{ color: "var(--text)" }}>Agents</span>
        </div>
        <h1 className="page-title">Agents</h1>
        <div className="page-sub tabular">{count} agent{count === 1 ? "" : "s"} · LLM × system prompt × tools</div>
      </div>
      <div className="page-actions">
        <Btn icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>
        <Btn icon="plus" kind="primary" onClick={onNew}>New agent</Btn>
      </div>
    </div>
  );
}

// ============================================================================
// Create modal
// ============================================================================

function NewAgentModal({ onClose, onCreate }) {
  const { push: pushToast } = useToast();
  const providers = useResource("agents:llm-providers",
    (s) => apiFetch("GET", "/llm_providers?limit=200", null, { signal: s }), {});
  const toolsets = useResource("agents:toolsets",
    (s) => apiFetch("GET", "/toolsets?limit=200", null, { signal: s }), {});

  const [id, setId] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [providerId, setProviderId] = React.useState("");
  const [modelName, setModelName] = React.useState("");
  const [systemPrompt, setSystemPrompt] = React.useState("");
  const [selectedTools, setSelectedTools] = React.useState([]);
  const [temperature, setTemperature] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState({});

  React.useEffect(() => {
    if (!providerId && providers.data?.items?.length) setProviderId(providers.data.items[0].id);
  }, [providers.data, providerId]);
  const selectedProvider = (providers.data?.items ?? []).find((p) => p.id === providerId);
  const modelOptions = selectedProvider?.models ?? [];
  React.useEffect(() => {
    if (modelOptions.length > 0 && !modelOptions.some((m) => m.name === modelName)) {
      setModelName(modelOptions[0].name);
    }
  }, [modelOptions]);  // eslint-disable-line react-hooks/exhaustive-deps

  const toggleTool = (tid) => {
    setSelectedTools((prev) => prev.includes(tid) ? prev.filter((x) => x !== tid) : [...prev, tid]);
  };

  const create = useMutation(
    (body) => apiFetch("POST", "/agents", body),
    {
      invalidates: ["agents:list"],
      onSuccess: (a) => onCreate(a),
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

  const submit = async () => {
    setFieldErrors({});
    const body = {
      ...(id ? { id } : {}),
      description: description || "(no description)",
      model: { provider_id: providerId, model_name: modelName },
      tools: selectedTools,
      system_prompt: systemPrompt ? [systemPrompt] : [],
    };
    if (temperature !== "" && !Number.isNaN(+temperature)) {
      body.temperature = Number(temperature);
    }
    try { await create.mutate(body); } catch (_e) {}
  };

  return (
    <Modal
      title="New agent"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit} disabled={!providerId || !modelName || create.loading}>
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
        <label className="field-label">Description</label>
        <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} style={{ width: "100%" }} />
        {fieldErrors["body.description"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.description"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">LLM provider</label>
        <select className="select" value={providerId} onChange={(e) => setProviderId(e.target.value)} style={{ width: "100%" }}>
          <option value="">-- pick a provider --</option>
          {(providers.data?.items ?? []).map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        {(providers.data?.items ?? []).length === 0 && !providers.loading && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No LLM providers configured. Create one at <span className="mono">/providers/llm</span> first.
          </div>
        )}
        {fieldErrors["body.model.provider_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.model.provider_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Model</label>
        <select className="select" value={modelName} onChange={(e) => setModelName(e.target.value)} style={{ width: "100%" }}>
          <option value="">-- pick a model --</option>
          {modelOptions.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
        </select>
        <div className="field-help">Model list comes from the provider row, not a live introspection (T0025).</div>
        {fieldErrors["body.model.model_name"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.model.model_name"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Toolsets <span className="hint">optional</span></label>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {(toolsets.data?.items ?? []).map((t) => (
            <span
              key={t.id}
              className={`chip ${selectedTools.includes(t.id) ? "active" : ""}`}
              onClick={() => toggleTool(t.id)}
              style={{ cursor: "pointer" }}
            >{t.id}</span>
          ))}
          {(toolsets.data?.items ?? []).length === 0 && !toolsets.loading && (
            <span className="muted text-sm">No toolsets configured.</span>
          )}
        </div>
      </div>
      <div className="field">
        <label className="field-label">System prompt <span className="hint">optional · stored as a single-segment list</span></label>
        <textarea className="textarea" value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} rows={4} />
      </div>
      <div className="field">
        <label className="field-label">Temperature <span className="hint">optional · default is provider-decided</span></label>
        <input className="input" type="number" step="0.05" min="0" value={temperature} onChange={(e) => setTemperature(e.target.value)} style={{ width: 100 }} />
        {fieldErrors["body.temperature"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.temperature"]}</div>}
      </div>
    </Modal>
  );
}

// ============================================================================
// Agent detail
// ============================================================================

const AGENT_TABS = [
  { id: "config",   label: "Config",   icon: "settings" },
  { id: "tools",    label: "Tools",    icon: "tools" },
  { id: "sessions", label: "Sessions", icon: "zap" },
  { id: "metadata", label: "Metadata", icon: "info" },
];

function AgentDetail() {
  const { params, query: routerQuery, navigate } = useRouter();
  const { push: pushToast } = useToast();
  const id = params.id;
  const tab = AGENT_TABS.some((t) => t.id === routerQuery.tab) ? routerQuery.tab : "config";

  const agent = useResource("agent-detail:" + id,
    (s) => apiFetch("GET", "/agents/" + encodeURIComponent(id), null, { signal: s }),
    { pollMs: null, deps: [id] });
  const status = useResource("agent-status:" + id,
    (s) => apiFetch("GET", "/agents/" + encodeURIComponent(id) + "/status", null, { signal: s }),
    { pollMs: 30000, deps: [id] });

  const delMut = useMutation(
    () => apiFetch("DELETE", "/agents/" + encodeURIComponent(id)),
    {
      invalidates: ["agents:list"],
      onSuccess: () => { pushToast({ kind: "warning", title: "Agent deleted", detail: id }); navigate("/agents"); },
      onError: (err) => pushToast({ kind: "error", title: "Delete failed", detail: err.detail || err.message, requestId: err.requestId }),
    }
  );
  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [newSessionOpen, setNewSessionOpen] = React.useState(false);

  if (agent.loading && !agent.data) {
    return <>
      <AgentDetailHeader id={id} navigate={navigate} />
      <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
    </>;
  }
  if (agent.error && !agent.data) {
    return <>
      <AgentDetailHeader id={id} navigate={navigate} />
      <Banner kind="error" title={agent.error.title || "Couldn't load agent"} detail={agent.error.detail || agent.error.message}
        actions={<Btn size="sm" icon="chevron-left" onClick={() => navigate("/agents")}>Back to list</Btn>} />
    </>;
  }
  const a = agent.data;
  const setTab = (t) => navigate("/agents/" + id, { tab: t });

  return (
    <div className="col" style={{ gap: 14 }}>
      <AgentDetailHeader
        id={id}
        navigate={navigate}
        onTest={() => setNewSessionOpen(true)}
        onDelete={() => setConfirmDelete(true)}
      />

      {/* Status panel */}
      <StatusPanel id={id} status={status} />

      <div className="panel">
        <div style={{ display: "flex", alignItems: "center", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {AGENT_TABS.map((t) => (
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
          {tab === "config" && <AgentConfigTab agent={a} />}
          {tab === "tools" && <AgentToolsTab agent={a} />}
          {tab === "sessions" && <AgentSessionsTab agentId={id} />}
          {tab === "metadata" && <AgentMetadataTab agent={a} />}
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
            <li>Removes the agent row from storage.</li>
            <li>Any session bound to this agent that is still running will fail on the next turn-claim.</li>
            <li>DELETE is NOT idempotent — a second DELETE returns 404 (app spec §5).</li>
          </ul>
        </Modal>
      )}

      {newSessionOpen && (
        <NewSessionModal
          onClose={() => setNewSessionOpen(false)}
          defaultAgentId={id}
          onCreate={() => { setNewSessionOpen(false); }}
        />
      )}
    </div>
  );
}

function AgentDetailHeader({ id, navigate, onTest, onDelete }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="crumb">
          <a onClick={() => navigate("/agents")}>Agents</a>
          <span className="sep">/</span>
          <span className="mono" style={{ color: "var(--text)" }}>{id}</span>
        </div>
        <h1 className="page-title mono">{id}</h1>
      </div>
      <div className="page-actions">
        {onTest && <Btn icon="play" kind="primary" onClick={onTest}>Test agent</Btn>}
        {onDelete && <Btn icon="trash" kind="danger" onClick={onDelete}>Delete</Btn>}
        <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/agents")}>Back</Btn>
      </div>
    </div>
  );
}

function StatusPanel({ id, status }) {
  const ok = status.data?.ok === true;
  const issues = status.data?.issues || [];
  const colour = status.data == null ? "var(--text-3)" : ok ? "var(--green)" : "var(--red)";
  return (
    <div className="panel" style={{
      background: ok ? "linear-gradient(90deg, var(--green-dim) 0%, var(--bg-1) 50%)"
                     : status.data == null ? undefined
                                            : "linear-gradient(90deg, var(--red-dim) 0%, var(--bg-1) 50%)",
      borderColor: ok ? "oklch(0.75 0.15 145 / 0.3)" : (status.data == null ? undefined : "oklch(0.7 0.2 25 / 0.3)"),
    }}>
      <div className="panel-body" style={{ display: "flex", alignItems: "flex-start", gap: 14, padding: "14px 18px" }}>
        <Icon name={ok ? "check-circle" : status.data == null ? "info" : "x-circle"} size={28} style={{ color: colour, flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>
            {status.loading && status.data == null ? "Checking references…"
              : status.error ? "Status check failed"
              : ok ? "All references resolve"
              : `${issues.length} issue${issues.length === 1 ? "" : "s"} blocking new sessions`}
          </div>
          <div className="muted text-sm">
            <span className="mono">GET /v1/agents/{id}/status</span>
            {" · "}polled every 30s
            {status.error && <> · <span style={{ color: "var(--red)" }}>{status.error.title || status.error.message}</span></>}
          </div>
          {issues.length > 0 && (
            <div className="mt-2">
              {issues.map((iss, i) => (
                <div key={i} className="ref-row" style={{ borderColor: "var(--red-dim)" }}>
                  <Icon name="alert" size={12} className="ico" style={{ color: "var(--red)" }} />
                  <span className="label" style={{ color: "var(--red)" }}>{iss.kind || "issue"}</span>
                  <span className="val">{iss.detail || iss.message || JSON.stringify(iss)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Config tab
// ============================================================================

function AgentConfigTab({ agent }) {
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        Read-only render. PUT-replace edit deferred to v2; use DELETE + POST
        for now. References panel below cross-checks the bound provider +
        toolsets.
      </div>
      <div className="code-block" dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(agent, null, 2)) }} />
      <ReferencesPanel agent={agent} />
    </div>
  );
}

function ReferencesPanel({ agent }) {
  const { navigate } = useRouter();
  const providerId = agent.model?.provider_id;
  const provider = useResource(providerId ? `llm-provider:${providerId}` : "llm-provider:none",
    (s) => providerId ? apiFetch("GET", "/llm_providers/" + encodeURIComponent(providerId), null, { signal: s }) : Promise.resolve(null),
    { pollMs: null, deps: [providerId] });

  return (
    <div className="mt-3 panel">
      <div className="panel-h">
        <Icon name="fork" size={13} />
        <span>References</span>
      </div>
      <div className="panel-body" style={{ padding: "4px 14px" }}>
        <div className="ref-row">
          <Icon name="llm" size={13} className="ico" />
          <span className="label">LLM provider</span>
          <span className="val"><a onClick={() => providerId && navigate("/providers/llm/" + providerId)}>{providerId || "—"}</a></span>
          {provider.loading
            ? <span className="muted text-sm">checking…</span>
            : provider.error?.status === 404
              ? <span className="pill pill-failed"><span className="dot"></span>missing</span>
              : provider.data
                ? <span className="pill pill-ended"><span className="dot"></span>ok</span>
                : null}
        </div>
        {(agent.tools || []).map((tsId) => (
          <ToolsetRefRow key={tsId} tsId={tsId} navigate={navigate} />
        ))}
        {(agent.tools || []).length === 0 && (
          <div className="ref-row">
            <Icon name="tools" size={13} className="ico" />
            <span className="label">Toolsets</span>
            <span className="val muted">none</span>
          </div>
        )}
      </div>
    </div>
  );
}

function ToolsetRefRow({ tsId, navigate }) {
  const tools = useResource(`toolset-tools:${tsId}`,
    (s) => apiFetch("GET", "/toolsets/" + encodeURIComponent(tsId) + "/tools", null, { signal: s }),
    { pollMs: null, deps: [tsId] });
  const count = tools.data?.tools?.length;
  const t711 = tools.error?.status === 500;
  return (
    <div className="ref-row">
      <Icon name="tools" size={13} className="ico" />
      <span className="label">Toolset</span>
      <span className="val">
        <a onClick={() => !tsId.startsWith("_") && tsId !== "web" && navigate("/toolsets/" + tsId)}>{tsId}</a>
        {count != null && <span className="muted text-sm"> · {count} tool{count === 1 ? "" : "s"}</span>}
      </span>
      {tools.loading
        ? <span className="muted text-sm">…</span>
        : t711
          ? <span className="pill pill-failed" title="T0711 — MCP-HTTP 500 leak"><span className="dot"></span>T0711</span>
          : tools.error
            ? <span className="pill pill-failed"><span className="dot"></span>err</span>
            : <span className="pill pill-ended"><span className="dot"></span>ok</span>}
    </div>
  );
}

// ============================================================================
// Tools tab — flattened union of every tool exposed by every toolset
// ============================================================================

function AgentToolsTab({ agent }) {
  const toolsets = agent.tools || [];
  if (toolsets.length === 0) {
    return <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>No toolsets bound to this agent.</div>;
  }
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">
        Flattened union of every tool exposed by this agent's {toolsets.length} bound
        toolset{toolsets.length === 1 ? "" : "s"}. Each card lists the canonical
        tool <span className="mono">id</span> (T0140/T0141 — not <span className="mono">name</span>).
      </div>
      {toolsets.map((tsId) => <ToolsetSection key={tsId} tsId={tsId} />)}
    </div>
  );
}

function ToolsetSection({ tsId }) {
  const tools = useResource(`toolset-tools:${tsId}`,
    (s) => apiFetch("GET", "/toolsets/" + encodeURIComponent(tsId) + "/tools", null, { signal: s }),
    { pollMs: null, deps: [tsId] });

  // T0711 MCP-HTTP leak — for any toolset returning 500, surface the
  // anomaly block instead of crashing the rest of the page.
  if (tools.error?.status === 500) {
    return (
      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="panel-h">
          <Icon name="tools" size={12} className="muted" />
          <span className="mono">{tsId}</span>
          <span className="pill pill-failed" style={{ marginLeft: 6 }}><span className="dot"></span>T0711</span>
        </div>
        <div className="panel-body">
          <Banner
            kind="error"
            title="Tools list unavailable"
            detail="The documented bug pinned by T0711 — MCP-HTTP transport leaks 500/errors/internal when the remote server is unreachable. Visit the toolset detail to Invalidate the cached provider and retry."
            actions={<Btn size="sm" icon="refresh" onClick={tools.refetch}>Retry</Btn>}
          />
        </div>
      </div>
    );
  }
  if (tools.error) {
    return (
      <div className="panel" style={{ marginBottom: 14 }}>
        <div className="panel-h">
          <Icon name="tools" size={12} className="muted" />
          <span className="mono">{tsId}</span>
          <span className="pill pill-failed" style={{ marginLeft: 6 }}><span className="dot"></span>error</span>
        </div>
        <div className="panel-body">
          <Banner kind="error" title={tools.error.title || "Couldn't load tools"} detail={tools.error.detail || tools.error.message}
            actions={<Btn size="sm" icon="refresh" onClick={tools.refetch}>Retry</Btn>} />
        </div>
      </div>
    );
  }
  if (tools.loading && !tools.data) {
    return <div className="panel" style={{ marginBottom: 14 }}>
      <div className="panel-h"><Icon name="tools" size={12} className="muted" /><span className="mono">{tsId}</span></div>
      <div className="panel-body"><div className="muted text-sm" style={{ textAlign: "center" }}>Loading…</div></div>
    </div>;
  }
  const items = tools.data?.tools || [];
  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      <div className="panel-h">
        <Icon name="tools" size={12} className="muted" />
        <span className="mono">{tsId}</span>
        <span className="sub">· {items.length} tool{items.length === 1 ? "" : "s"}</span>
      </div>
      <div className="panel-body" style={{ padding: 0 }}>
        {items.length === 0 ? (
          <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>No tools.</div>
        ) : items.map((tool, i) => <ToolEntry key={tool.id || i} tool={tool} />)}
      </div>
    </div>
  );
}

function ToolEntry({ tool }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div style={{ borderBottom: "1px solid var(--border)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 14px", cursor: "pointer" }} onClick={() => setOpen(!open)}>
        <Icon name={open ? "chevron-down" : "chevron-right"} size={11} className="muted" />
        <span className="mono" style={{ flex: 1, minWidth: 0 }}>{tool.id}</span>
        {tool.description && <span className="muted text-sm" style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{tool.description}</span>}
        <Btn size="sm" kind="ghost" disabled title="Tool invocation endpoint not yet implemented (planned — backend-additions §2.2)">Test call</Btn>
      </div>
      {open && tool.schema && (
        <div style={{ padding: "8px 14px 12px" }}>
          <div className="code-block" dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(tool.schema, null, 2)) }} />
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Sessions tab
// ============================================================================

function AgentSessionsTab({ agentId }) {
  const { navigate } = useRouter();
  const sessions = useResource(`agent-sessions:${agentId}`,
    (s) => apiFetch("GET", "/sessions?agent_id=" + encodeURIComponent(agentId) + "&limit=200", null, { signal: s }),
    { pollMs: 5000, deps: [agentId] });
  const items = sessions.data?.items ?? [];
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">Sessions bound to <span className="mono">{agentId}</span>, server-filtered.</div>
      {sessions.loading && items.length === 0 ? (
        <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>Loading…</div>
      ) : items.length === 0 ? (
        <div className="empty" style={{ padding: "30px 20px" }}>
          <div className="ico-wrap"><Icon name="zap" size={18} /></div>
          <div className="head">No sessions</div>
          <div className="sub">Use the Test agent button above to start one.</div>
        </div>
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead><tr><th>Status</th><th>Session</th><th>Workspace</th><th>Turns</th><th>Created</th></tr></thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id} onClick={() => navigate("/sessions/" + s.id)} style={{ cursor: "pointer" }}>
                  <td><StatusPill status={s.status} /></td>
                  <td className="mono">{s.id}</td>
                  <td className="mono muted">{(s.workspace_id || "").slice(0, 18)}{s.workspace_id && s.workspace_id.length > 18 ? "…" : ""}</td>
                  <td className="mono num tabular">{s.turn_count ?? 0}</td>
                  <td className="mono muted">{s.created_at ? relativeTime((Date.now() - new Date(s.created_at).getTime()) / 1000) : "—"}</td>
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
// Metadata tab
// ============================================================================

function AgentMetadataTab({ agent }) {
  const meta = agent.metadata || {};
  const keys = Object.keys(meta);
  return (
    <div style={{ padding: 14 }}>
      <div className="muted text-sm mb-3">Free-form key/value bag on the agent row. {keys.length} key{keys.length === 1 ? "" : "s"}.</div>
      {keys.length === 0 ? (
        <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>No metadata.</div>
      ) : (
        <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
          {keys.map((k) => (
            <React.Fragment key={k}>
              <dt>{k}</dt>
              <dd className="mono">{typeof meta[k] === "object" ? JSON.stringify(meta[k]) : String(meta[k])}</dd>
            </React.Fragment>
          ))}
        </dl>
      )}
    </div>
  );
}

window.AgentsPage = AgentsPage;
window.AgentDetail = AgentDetail;
