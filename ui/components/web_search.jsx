/* global React, Icon, Btn, Modal, Banner */

// Web Search providers — wired to the real API.
//
// Endpoints (CLAUDE.md §3.x):
//   GET    /v1/web_search_providers          — list (5s poll)
//   POST   /v1/web_search_providers          — create
//   PUT    /v1/web_search_providers/{id}     — update
//   DELETE /v1/web_search_providers/{id}     — delete (403 reserved; 409 cascade)
//   POST   /v1/web_search_providers/_test    — probe a draft / existing config
//   GET    /v1/web_search_providers/_types   — type → config_fields map
//   GET    /v1/web_search_active_config      — singleton (503 if unbootstrapped)
//   PUT    /v1/web_search_active_config      — replace
//
// Cache keys (matching the "<entity>:<thing>" convention used by
// channels.jsx and semantic-search.jsx):
//   web-search-providers:list     — providers list
//   web-search-active-config      — active config singleton
//   web-search-provider-types     — type-driven config field map
//
// Babel-standalone shares the global scope across <script> tags so
// every top-level binding here is prefixed with WSP_ (Web Search
// Provider) to avoid clashes with WS_* (workspaces.jsx), TS_* (toolsets),
// IC_* (internal collections), etc.

const WSP_CACHE_LIST = "web-search-providers:list";
const WSP_CACHE_ACTIVE = "web-search-active-config";
const WSP_CACHE_TYPES = "web-search-provider-types";


function _wspToastErr(pushToast, fallbackTitle) {
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


function _wspProviderTypeColor(type) {
  switch (type) {
    case "duckduckgo": return "var(--blue)";
    case "tavily": return "var(--violet)";
    default: return "var(--text-3)";
  }
}


function WSP_ProviderBadge({ type }) {
  const color = _wspProviderTypeColor(type);
  return (
    <span className="pill" style={{ background: "var(--bg-2)", color, border: "1px solid var(--border)" }}>
      <span className="dot" style={{ background: color }}></span>
      <span className="mono text-sm">{type}</span>
    </span>
  );
}


// ----------------------------------------------------------------------
// Top-level page
// ----------------------------------------------------------------------


function WebSearchPage({ pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;

  const active = useResource(
    WSP_CACHE_ACTIVE,
    (signal) => apiFetch("GET", "/web_search_active_config", null, { signal }),
    { pollMs: 5000 },
  );
  const providers = useResource(
    WSP_CACHE_LIST,
    (signal) => apiFetch("GET", "/web_search_providers?limit=200", null, { signal }),
    { pollMs: 5000 },
  );

  const [editing, setEditing] = React.useState(null);  // null | { row | null }
  const [editingActive, setEditingActive] = React.useState(false);
  const [deleteTarget, setDeleteTarget] = React.useState(null);
  const [deleteError, setDeleteError] = React.useState(null);

  const deleteMut = useMutation(
    () => apiFetch(
      "DELETE",
      `/web_search_providers/${encodeURIComponent(deleteTarget.id)}`,
    ),
    {
      invalidates: [WSP_CACHE_LIST],
      onSuccess: () => {
        const id = deleteTarget?.id;
        setDeleteTarget(null);
        setDeleteError(null);
        providers.refetch();
        if (pushToast) {
          pushToast({
            kind: "success",
            title: "Provider deleted",
            detail: `${id} (DELETE /v1/web_search_providers → 204)`,
          });
        }
      },
      onError: (err) => {
        if (err?.status === 409 && err?.body?.detail?.error === "cascade_blocked") {
          setDeleteError(err.body.detail);
          return;
        }
        _wspToastErr(pushToast, "Delete failed")(err);
      },
    },
  );

  const testExisting = useMutation(
    (row) => apiFetch("POST", "/web_search_providers/_test", row),
    {
      onSuccess: (resp, row) => {
        if (resp?.ok) {
          if (pushToast) {
            pushToast({
              kind: "success",
              title: `${row.id}: probe OK`,
              detail: resp.hits?.[0]?.title || "(no hits)",
            });
          }
        } else if (pushToast) {
          pushToast({
            kind: "error",
            title: `${row.id}: probe failed`,
            detail: resp?.error || "(no error)",
          });
        }
      },
      onError: _wspToastErr(pushToast, "Test failed"),
    },
  );

  const providerRows = providers.data?.items ?? [];

  return (
    <>
      <div className="col" style={{ gap: 14 }}>
        <WSP_ActiveConfigCard
          active={active}
          onEdit={() => setEditingActive(true)}
        />

        <WSP_ProvidersPanel
          providers={providerRows}
          loading={providers.loading}
          error={providers.error}
          onCreate={() => setEditing({ row: null })}
          onEdit={(row) => setEditing({ row })}
          onDelete={(row) => { setDeleteTarget(row); setDeleteError(null); }}
          onTest={(row) => testExisting.mutate(row)}
          onRetry={providers.refetch}
        />
      </div>

      {deleteTarget && (
        <Modal
          title={`Delete ${deleteTarget.id}?`}
          onClose={() => { setDeleteTarget(null); setDeleteError(null); }}
          footer={
            <>
              <Btn kind="ghost" onClick={() => { setDeleteTarget(null); setDeleteError(null); }}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                onClick={() => deleteMut.mutate()}
                disabled={!!deleteError || deleteMut.loading}
              >
                {deleteMut.loading ? "Deleting…" : "Delete"}
              </Btn>
            </>
          }
        >
          {deleteError ? (
            <Banner
              kind="error"
              title="Currently referenced by the active config"
              detail={deleteError.message || "Update the active config first, then retry."}
              actions={
                <Btn
                  size="sm"
                  icon="settings"
                  onClick={() => { setDeleteTarget(null); setDeleteError(null); setEditingActive(true); }}
                >
                  Edit active config
                </Btn>
              }
            />
          ) : (
            <div className="text-sm muted">
              This deletes the provider row. Cannot be undone.
            </div>
          )}
        </Modal>
      )}

      {editingActive && (
        <WSP_ActiveConfigModal
          active={active.data}
          providers={providerRows}
          onClose={() => setEditingActive(false)}
          onSaved={() => {
            setEditingActive(false);
            active.refetch();
            providers.refetch();
          }}
          pushToast={pushToast}
        />
      )}

      {editing !== null && (
        <WSP_ProviderEditModal
          row={editing.row}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            providers.refetch();
          }}
          pushToast={pushToast}
        />
      )}
    </>
  );
}


// ----------------------------------------------------------------------
// Active-config card
// ----------------------------------------------------------------------


function WSP_ActiveConfigCard({ active, onEdit }) {
  const data = active.data;

  if (active.loading && !data) {
    return (
      <div className="panel">
        <div className="muted text-sm" style={{ padding: 14 }}>Loading active configuration…</div>
      </div>
    );
  }

  if (active.error) {
    const is503 = active.error.status === 503;
    return (
      <Banner
        kind={is503 ? "warning" : "error"}
        title={is503 ? "Web search subsystem not bootstrapped" : (active.error.title || "Couldn't load active config")}
        detail={
          is503
            ? "Bootstrap should run automatically at server start; check server logs for failures."
            : (active.error.detail || active.error.message)
        }
        actions={<Btn size="sm" icon="refresh" onClick={active.refetch}>Retry</Btn>}
      />
    );
  }

  const cfg = data?.config;
  const isSingle = cfg?.mode === "single";
  const ids = isSingle ? [cfg.provider_id] : (cfg?.provider_ids ?? []);

  return (
    <div className="panel">
      <div style={{ display: "flex", alignItems: "center", padding: "12px 14px", borderBottom: "1px solid var(--border)", gap: 10 }}>
        <Icon name="globe" size={14} className="muted" />
        <div className="mono" style={{ fontSize: 12, fontWeight: 600 }}>Active web search</div>
        <span className="muted text-sm" style={{ marginLeft: 6 }}>· /v1/web_search_active_config</span>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="ghost" icon="edit" onClick={onEdit}>Edit</Btn>
        </div>
      </div>
      <div style={{ padding: 14 }}>
        <div className="kv" style={{ gridTemplateColumns: "120px 1fr", rowGap: 6 }}>
          <dt>mode</dt>
          <dd className="mono">{cfg?.mode || "—"}</dd>
          {isSingle ? (
            <>
              <dt>provider</dt>
              <dd className="mono">
                {cfg.provider_id}
                {cfg.provider_id === "DuckDuckGo" && (
                  <span className="pill" style={{ marginLeft: 8, background: "var(--bg-2)", color: "var(--text-3)", border: "1px solid var(--border)" }}>
                    <span className="mono text-sm">built-in</span>
                  </span>
                )}
              </dd>
            </>
          ) : (
            <>
              <dt>order</dt>
              <dd>
                {ids.length === 0 ? (
                  <span className="muted text-sm">(no providers)</span>
                ) : (
                  <ol className="mono" style={{ margin: 0, paddingLeft: 18 }}>
                    {ids.map((pid) => (
                      <li key={pid} style={{ padding: "2px 0" }}>
                        {pid}
                        {pid === "DuckDuckGo" && (
                          <span className="pill" style={{ marginLeft: 8, background: "var(--bg-2)", color: "var(--text-3)", border: "1px solid var(--border)" }}>
                            <span className="mono text-sm">built-in</span>
                          </span>
                        )}
                      </li>
                    ))}
                  </ol>
                )}
              </dd>
            </>
          )}
        </div>
      </div>
    </div>
  );
}


// ----------------------------------------------------------------------
// Providers panel (table)
// ----------------------------------------------------------------------


function WSP_ProvidersPanel({ providers, loading, error, onCreate, onEdit, onDelete, onTest, onRetry }) {
  return (
    <>
      <div className="filter-bar">
        <div className="mono" style={{ fontSize: 12, fontWeight: 600 }}>
          <Icon name="server" size={13} className="muted" /> Providers
        </div>
        <span className="muted text-sm" style={{ marginLeft: 4 }}>· /v1/web_search_providers every 5s</span>
        <span className="muted text-sm tabular" style={{ marginLeft: "auto" }}>
          <span className="mono" style={{ color: "var(--green)" }}>● live</span>
        </span>
        <Btn size="sm" kind="primary" icon="plus" onClick={onCreate}>New provider</Btn>
      </div>

      {error && providers.length === 0 ? (
        <Banner
          kind="error"
          title={error.title || "Couldn't load providers"}
          detail={error.detail || error.message}
          actions={<Btn size="sm" icon="refresh" onClick={onRetry}>Retry</Btn>}
        />
      ) : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>ID</th>
                <th>Type</th>
                <th>Status</th>
                <th style={{ textAlign: "right" }}></th>
              </tr>
            </thead>
            <tbody>
              {loading && providers.length === 0 ? (
                <tr><td colSpan={4} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</td></tr>
              ) : providers.length === 0 ? (
                <tr><td colSpan={4} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                  No web search providers configured.
                </td></tr>
              ) : providers.map((p) => {
                const reserved = p.id === "DuckDuckGo";
                return (
                  <tr key={p.id}>
                    <td className="mono">{p.id}</td>
                    <td><WSP_ProviderBadge type={p.provider_type} /></td>
                    <td>
                      {reserved ? (
                        <span className="pill" style={{ background: "var(--bg-2)", color: "var(--text-3)", border: "1px solid var(--border)" }}>
                          <span className="mono text-sm">built-in</span>
                        </span>
                      ) : (
                        <span className="muted text-sm">configured</span>
                      )}
                    </td>
                    <td style={{ textAlign: "right", paddingRight: 12 }}>
                      <div style={{ display: "inline-flex", gap: 6 }}>
                        <Btn size="sm" kind="ghost" icon="play" onClick={() => onTest(p)}>Test</Btn>
                        {!reserved && (
                          <>
                            <Btn size="sm" kind="ghost" icon="edit" onClick={() => onEdit(p)}>Edit</Btn>
                            <Btn size="sm" kind="ghost" icon="trash" onClick={() => onDelete(p)}>Delete</Btn>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}


// ----------------------------------------------------------------------
// Provider create / edit modal
// ----------------------------------------------------------------------


function WSP_ProviderEditModal({ row, onClose, onSaved, pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const isEdit = row !== null;

  const types = useResource(
    WSP_CACHE_TYPES,
    (s) => apiFetch("GET", "/web_search_providers/_types", null, { signal: s }),
  );

  const [id, setId] = React.useState(isEdit ? row.id : "");
  const [providerType, setProviderType] = React.useState(
    isEdit ? row.provider_type : "duckduckgo",
  );
  const [apiKey, setApiKey] = React.useState("");
  const [testResult, setTestResult] = React.useState(null);
  const [fieldErrors, setFieldErrors] = React.useState({});

  const fields = types.data?.[providerType]?.config_fields ?? [];

  const buildBody = () => ({
    id,
    provider_type: providerType,
    config: { type: providerType, ...(providerType === "tavily" ? { api_key: apiKey } : {}) },
  });

  const canSubmit = !!id && (
    providerType === "duckduckgo" || (providerType === "tavily" && (isEdit || apiKey.length > 0))
  );

  const save = useMutation(
    () => {
      const body = buildBody();
      if (isEdit) {
        return apiFetch("PUT", `/web_search_providers/${encodeURIComponent(id)}`, body);
      }
      return apiFetch("POST", "/web_search_providers", body);
    },
    {
      invalidates: [WSP_CACHE_LIST],
      onSuccess: () => {
        if (pushToast) {
          pushToast({
            kind: isEdit ? "info" : "success",
            title: isEdit ? "Provider updated" : "Provider created",
            detail: `${id} (${providerType})`,
          });
        }
        onSaved();
      },
      onError: (err) => {
        if (err?.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) {
            const loc = (fe.loc || []).filter((seg) => seg !== "body");
            next[loc.join(".")] = fe.msg;
          }
          setFieldErrors(next);
          return;
        }
        _wspToastErr(pushToast, isEdit ? "Save failed" : "Create failed")(err);
      },
    },
  );

  const testDraft = useMutation(
    () => apiFetch("POST", "/web_search_providers/_test", buildBody()),
    {
      onSuccess: (resp) => setTestResult(resp),
      onError: (err) => setTestResult({ ok: false, error: err?.message || "request failed" }),
    },
  );

  return (
    <Modal
      title={isEdit ? `Edit web search provider · ${row.id}` : "New web search provider"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="ghost" icon="play" onClick={() => testDraft.mutate()} disabled={!canSubmit || testDraft.loading}>
            {testDraft.loading ? "Probing…" : "Test"}
          </Btn>
          <Btn kind="primary" icon={isEdit ? "check" : "plus"} onClick={() => save.mutate()} disabled={!canSubmit || save.loading}>
            {save.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create provider")}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">
          id {isEdit
            ? <span className="hint">locked — id cannot change after create</span>
            : <span className="hint">operator-chosen identifier</span>}
        </label>
        <input
          className="input mono"
          placeholder="my-tavily"
          value={id}
          onChange={(e) => setId(e.target.value)}
          disabled={isEdit}
          style={{ width: "100%" }}
        />
        {fieldErrors["id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["id"]}</div>}
      </div>

      <div className="field">
        <label className="field-label">
          type {isEdit && <span className="hint">locked — recreate to change type</span>}
        </label>
        <select
          className="select mono"
          value={providerType}
          onChange={(e) => { setProviderType(e.target.value); setApiKey(""); setTestResult(null); }}
          disabled={isEdit}
          style={{ width: "100%" }}
        >
          <option value="duckduckgo">duckduckgo</option>
          <option value="tavily">tavily</option>
        </select>
      </div>

      {fields.includes("api_key") && (
        <div className="field">
          <label className="field-label">
            api key {isEdit && <span className="hint">leave blank to keep current</span>}
          </label>
          <input
            className="input mono"
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={isEdit ? "(unchanged)" : "tvly-…"}
            style={{ width: "100%" }}
          />
          {fieldErrors["config.api_key"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["config.api_key"]}</div>}
        </div>
      )}

      {testResult && (
        <div style={{ marginTop: 10 }}>
          {testResult.ok ? (
            <Banner
              kind="success"
              title="Probe OK"
              detail={`First hit: ${testResult.hits?.[0]?.title ?? "(no hits)"}`}
            />
          ) : (
            <Banner
              kind="error"
              title="Probe failed"
              detail={testResult.error || "(no error)"}
            />
          )}
        </div>
      )}
    </Modal>
  );
}


// ----------------------------------------------------------------------
// Active-config modal
// ----------------------------------------------------------------------


function WSP_ActiveConfigModal({ active, providers, onClose, onSaved, pushToast }) {
  const { useMutation, apiFetch } = window.primerApi;

  const initialCfg = active?.config ?? { mode: "single", provider_id: "DuckDuckGo" };
  const [mode, setMode] = React.useState(initialCfg.mode);
  const [singleId, setSingleId] = React.useState(initialCfg.provider_id ?? "DuckDuckGo");
  const [aggIds, setAggIds] = React.useState(
    initialCfg.mode === "aggregated" ? (initialCfg.provider_ids ?? ["DuckDuckGo"]) : ["DuckDuckGo"],
  );

  const save = useMutation(
    () => {
      const config = mode === "single"
        ? { mode: "single", provider_id: singleId }
        : { mode: "aggregated", provider_ids: aggIds };
      return apiFetch("PUT", "/web_search_active_config", { config });
    },
    {
      invalidates: [WSP_CACHE_ACTIVE],
      onSuccess: () => {
        if (pushToast) {
          pushToast({ kind: "success", title: "Active config updated" });
        }
        onSaved();
      },
      onError: (err) => {
        const unknown = err?.body?.detail?.unknown_ids;
        if (unknown && pushToast) {
          pushToast({
            kind: "error",
            title: "Unknown provider id(s)",
            detail: unknown.join(", "),
          });
          return;
        }
        _wspToastErr(pushToast, "Save failed")(err);
      },
    },
  );

  const canSubmit = mode === "single"
    ? !!singleId
    : aggIds.length > 0;

  return (
    <Modal
      title="Active web search configuration"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="check" onClick={() => save.mutate()} disabled={!canSubmit || save.loading}>
            {save.loading ? "Saving…" : "Save changes"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">mode</label>
        <div className="row" style={{ gap: 14, paddingTop: 4 }}>
          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input
              type="radio" name="mode" value="single"
              checked={mode === "single"}
              onChange={() => setMode("single")}
            />
            <span className="mono">single</span>
            <span className="muted text-sm">— one provider, no fallback</span>
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input
              type="radio" name="mode" value="aggregated"
              checked={mode === "aggregated"}
              onChange={() => setMode("aggregated")}
            />
            <span className="mono">aggregated</span>
            <span className="muted text-sm">— priority-ordered fallback chain</span>
          </label>
        </div>
      </div>

      {mode === "single" && (
        <div className="field">
          <label className="field-label">provider</label>
          <select
            className="select mono"
            value={singleId}
            onChange={(e) => setSingleId(e.target.value)}
            style={{ width: "100%" }}
          >
            {providers.map((p) => (
              <option key={p.id} value={p.id}>{p.id}</option>
            ))}
          </select>
        </div>
      )}

      {mode === "aggregated" && (
        <WSP_AggregatedEditor
          aggIds={aggIds}
          setAggIds={setAggIds}
          providers={providers}
        />
      )}
    </Modal>
  );
}


function WSP_AggregatedEditor({ aggIds, setAggIds, providers }) {
  const known = new Set(providers.map((p) => p.id));
  const candidates = providers.map((p) => p.id).filter((pid) => !aggIds.includes(pid));

  const move = (idx, delta) => {
    const next = [...aggIds];
    const dst = idx + delta;
    if (dst < 0 || dst >= next.length) return;
    [next[idx], next[dst]] = [next[dst], next[idx]];
    setAggIds(next);
  };

  const remove = (idx) => setAggIds(aggIds.filter((_, i) => i !== idx));
  const add = (pid) => setAggIds([...aggIds, pid]);

  return (
    <div className="field">
      <label className="field-label">
        providers <span className="hint">index 0 is primary; on failure walks the list</span>
      </label>
      {aggIds.length === 0 ? (
        <Banner kind="warning" title="At least one provider is required" />
      ) : (
        <div className="tbl-wrap" style={{ marginTop: 6 }}>
          <table className="tbl">
            <tbody>
              {aggIds.map((pid, idx) => (
                <tr key={pid}>
                  <td className="mono muted text-sm" style={{ width: 40 }}>#{idx + 1}</td>
                  <td className="mono">
                    {pid}
                    {!known.has(pid) && (
                      <span className="pill" style={{ marginLeft: 8, background: "var(--bg-2)", color: "var(--amber)", border: "1px solid var(--amber)" }}>
                        <span className="mono text-sm">unknown</span>
                      </span>
                    )}
                  </td>
                  <td style={{ textAlign: "right", paddingRight: 12 }}>
                    <div style={{ display: "inline-flex", gap: 6 }}>
                      <Btn size="sm" kind="ghost" icon="chevron-up" onClick={() => move(idx, -1)} disabled={idx === 0}>Up</Btn>
                      <Btn size="sm" kind="ghost" icon="chevron-down" onClick={() => move(idx, +1)} disabled={idx === aggIds.length - 1}>Down</Btn>
                      <Btn size="sm" kind="ghost" icon="x" onClick={() => remove(idx)}>Remove</Btn>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {candidates.length > 0 && (
        <div style={{ marginTop: 10, display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <span className="muted text-sm">add:</span>
          {candidates.map((pid) => (
            <Btn key={pid} size="sm" kind="ghost" icon="plus" onClick={() => add(pid)}>{pid}</Btn>
          ))}
        </div>
      )}
    </div>
  );
}


window.WebSearchPage = WebSearchPage;
