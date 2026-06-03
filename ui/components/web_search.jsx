/* global React */

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

const WS_CACHE_LIST = "web-search-providers:list";
const WS_CACHE_ACTIVE = "web-search-active-config";
const WS_CACHE_TYPES = "web-search-provider-types";


function WebSearchPage({ pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;

  const active = useResource(
    WS_CACHE_ACTIVE,
    (signal) => apiFetch("GET", "/web_search_active_config", null, { signal }),
    { pollMs: 5000 },
  );
  const providers = useResource(
    WS_CACHE_LIST,
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
      invalidates: [WS_CACHE_LIST],
      onSuccess: () => {
        setDeleteTarget(null);
        providers.refetch();
        pushToast({ kind: "success", message: "Provider deleted." });
      },
      onError: (err) => {
        if (err.status === 409 && err.body?.detail?.error === "cascade_blocked") {
          setDeleteError(err.body.detail);
        } else {
          pushToast({ kind: "error", message: err.message });
        }
      },
    },
  );

  const testExisting = useMutation(
    (row) => apiFetch("POST", "/web_search_providers/_test", row),
    {
      onSuccess: (resp, row) => {
        if (resp.ok) {
          pushToast({ kind: "success", message: `${row.id}: OK -- ${resp.hits[0]?.title ?? "(no hits)"}` });
        } else {
          pushToast({ kind: "error", message: `${row.id}: ${resp.error}` });
        }
      },
    },
  );

  return (
    <div className="page">
      <ActiveConfigCard
        active={active}
        providers={providers.data?.items ?? []}
        onEdit={() => setEditingActive(true)}
      />

      <ProvidersTable
        providers={providers.data?.items ?? []}
        loading={providers.loading}
        error={providers.error}
        onCreate={() => setEditing({ row: null })}
        onEdit={(row) => setEditing({ row })}
        onDelete={(row) => { setDeleteTarget(row); setDeleteError(null); }}
        onTest={(row) => testExisting.mutate(row)}
        pushToast={pushToast}
      />

      {deleteTarget && (
        <div className="modal">
          <h3>Delete {deleteTarget.id}?</h3>
          {deleteError ? (
            <div className="error">
              <p>{deleteError.message}</p>
              <button onClick={() => setEditingActive(true)}>
                Go to active config
              </button>
            </div>
          ) : (
            <p>This cannot be undone.</p>
          )}
          <div className="modal-actions">
            <button onClick={() => { setDeleteTarget(null); setDeleteError(null); }}>
              Cancel
            </button>
            <button
              className="btn-danger"
              onClick={() => deleteMut.mutate()}
              disabled={!!deleteError}
            >Delete</button>
          </div>
        </div>
      )}

      {editingActive && (
        <ActiveConfigModal
          active={active.data}
          providers={providers.data?.items ?? []}
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
        <ProviderEditModal
          row={editing.row}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            providers.refetch();
          }}
          pushToast={pushToast}
        />
      )}
    </div>
  );
}


function ActiveConfigCard({ active, providers, onEdit }) {
  const data = active.data;
  if (active.loading && !data) {
    return <div className="card">Loading active config…</div>;
  }
  if (active.error) {
    return (
      <div className="card card-error">
        Could not load active web search config: {active.error.message}
        {active.error.status === 503 && (
          <p className="muted">
            (Subsystem not bootstrapped. Restart the server.)
          </p>
        )}
      </div>
    );
  }
  const cfg = data?.config;
  return (
    <div className="card">
      <h2>Active web search</h2>
      {cfg?.mode === "single" && (
        <div>
          <p>Mode: <strong>single</strong></p>
          <p>Provider: <code>{cfg.provider_id}</code></p>
        </div>
      )}
      {cfg?.mode === "aggregated" && (
        <div>
          <p>Mode: <strong>aggregated</strong></p>
          <p>Providers (in priority order):</p>
          <ol>
            {cfg.provider_ids.map((pid) => (
              <li key={pid}>
                <code>{pid}</code>
                {pid === "DuckDuckGo" && <span className="badge"> built-in</span>}
              </li>
            ))}
          </ol>
        </div>
      )}
      <button className="btn" onClick={onEdit}>Edit active config</button>
    </div>
  );
}


function ProvidersTable({ providers, loading, error, onCreate, onEdit, onDelete, onTest }) {
  return (
    <div className="card">
      <div className="card-header">
        <h2>Providers</h2>
        <button className="btn btn-primary" onClick={onCreate}>+ Add provider</button>
      </div>
      {error && <div className="error">{error.message}</div>}
      {loading && providers.length === 0 && <div>Loading…</div>}
      <table className="table">
        <thead>
          <tr><th>ID</th><th>Type</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>
          {providers.map((p) => {
            const reserved = p.id === "DuckDuckGo";
            return (
              <tr key={p.id}>
                <td><code>{p.id}</code></td>
                <td>{p.provider_type}</td>
                <td>
                  {reserved && <span className="badge">built-in</span>}
                  {!reserved && <span>configured</span>}
                </td>
                <td className="row-actions">
                  <button onClick={() => onTest(p)}>Test</button>
                  {!reserved && (
                    <>
                      <button onClick={() => onEdit(p)}>Edit</button>
                      <button onClick={() => onDelete(p)} className="btn-danger">Delete</button>
                    </>
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


function ProviderEditModal({ row, onClose, onSaved, pushToast }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const isEdit = row !== null;

  const types = useResource(
    WS_CACHE_TYPES,
    (s) => apiFetch("GET", "/web_search_providers/_types", null, { signal: s }),
  );

  const [id, setId] = React.useState(isEdit ? row.id : "");
  const [providerType, setProviderType] = React.useState(
    isEdit ? row.provider_type : "duckduckgo",
  );
  const [apiKey, setApiKey] = React.useState("");
  const [testResult, setTestResult] = React.useState(null);

  const fields = types.data?.[providerType]?.config_fields ?? [];

  const buildBody = () => ({
    id,
    provider_type: providerType,
    config: { type: providerType, ...(providerType === "tavily" ? { api_key: apiKey } : {}) },
  });

  const save = useMutation(
    () => {
      const body = buildBody();
      if (isEdit) {
        return apiFetch("PUT", `/web_search_providers/${encodeURIComponent(id)}`, body);
      }
      return apiFetch("POST", "/web_search_providers", body);
    },
    {
      invalidates: [WS_CACHE_LIST],
      onSuccess: () => {
        pushToast({ kind: "success", message: `Provider ${id} saved.` });
        onSaved();
      },
      onError: (err) => {
        pushToast({ kind: "error", message: err.message });
      },
    },
  );

  const testDraft = useMutation(
    () => apiFetch("POST", "/web_search_providers/_test", buildBody()),
    { onSuccess: (resp) => setTestResult(resp) },
  );

  return (
    <div className="modal">
      <h3>{isEdit ? `Edit ${row.id}` : "New web search provider"}</h3>

      <label>ID
        <input
          value={id}
          onChange={(e) => setId(e.target.value)}
          disabled={isEdit}
          placeholder="my-tavily"
        />
      </label>

      <label>Type
        <select
          value={providerType}
          onChange={(e) => setProviderType(e.target.value)}
          disabled={isEdit}
        >
          <option value="duckduckgo">duckduckgo</option>
          <option value="tavily">tavily</option>
        </select>
      </label>

      {fields.includes("api_key") && (
        <label>API key
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={isEdit ? "(unchanged)" : "tvly-..."}
          />
        </label>
      )}

      {testResult && (
        <div className={testResult.ok ? "test-ok" : "test-fail"}>
          {testResult.ok
            ? `Test OK -- first hit: ${testResult.hits[0]?.title ?? "(no hits)"}`
            : `Test failed: ${testResult.error}`
          }
        </div>
      )}

      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button onClick={() => testDraft.mutate()}>Test</button>
        <button
          className="btn-primary"
          onClick={() => save.mutate()}
          disabled={save.loading || !id}
        >Save</button>
      </div>
    </div>
  );
}
function ActiveConfigModal({ active, providers, onClose, onSaved, pushToast }) {
  const { useMutation, apiFetch } = window.primerApi;

  const initial = active?.config ?? { mode: "single", provider_id: "DuckDuckGo" };
  const [mode, setMode] = React.useState(initial.mode);
  const [singleId, setSingleId] = React.useState(initial.provider_id ?? "DuckDuckGo");
  const [aggIds, setAggIds] = React.useState(initial.provider_ids ?? ["DuckDuckGo"]);

  const save = useMutation(
    () => {
      const config = mode === "single"
        ? { mode: "single", provider_id: singleId }
        : { mode: "aggregated", provider_ids: aggIds };
      return apiFetch("PUT", "/web_search_active_config", { config });
    },
    {
      invalidates: [WS_CACHE_ACTIVE],
      onSuccess: () => {
        pushToast({ kind: "success", message: "Active config updated." });
        onSaved();
      },
      onError: (err) => {
        const unknown = err.body?.detail?.unknown_ids;
        if (unknown) {
          pushToast({ kind: "error", message: `Unknown provider id(s): ${unknown.join(", ")}` });
        } else {
          pushToast({ kind: "error", message: err.message });
        }
      },
    },
  );

  return (
    <div className="modal">
      <h3>Active web search config</h3>

      <fieldset>
        <legend>Mode</legend>
        <label><input
          type="radio" name="mode" value="single"
          checked={mode === "single"}
          onChange={() => setMode("single")}
        /> Single</label>
        <label><input
          type="radio" name="mode" value="aggregated"
          checked={mode === "aggregated"}
          onChange={() => setMode("aggregated")}
        /> Aggregated (priority-ordered fallback)</label>
      </fieldset>

      {mode === "single" && (
        <label>Provider
          <select value={singleId} onChange={(e) => setSingleId(e.target.value)}>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>{p.id}</option>
            ))}
          </select>
        </label>
      )}

      {mode === "aggregated" && (
        <AggregatedEditor
          aggIds={aggIds}
          setAggIds={setAggIds}
          providers={providers}
        />
      )}

      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button
          className="btn-primary"
          onClick={() => save.mutate()}
          disabled={save.loading || (mode === "aggregated" && aggIds.length === 0)}
        >Save</button>
      </div>
    </div>
  );
}

function AggregatedEditor({ aggIds, setAggIds, providers }) {
  const known = new Set(providers.map((p) => p.id));
  const candidates = providers.map((p) => p.id).filter((pid) => !aggIds.includes(pid));

  const move = (idx, delta) => {
    const next = [...aggIds];
    const dst = idx + delta;
    if (dst < 0 || dst >= next.length) return;
    [next[idx], next[dst]] = [next[dst], next[idx]];
    setAggIds(next);
  };

  const remove = (idx) => {
    setAggIds(aggIds.filter((_, i) => i !== idx));
  };

  const add = (pid) => {
    setAggIds([...aggIds, pid]);
  };

  return (
    <div>
      <p>Providers in priority order (index 0 is primary):</p>
      <ol>
        {aggIds.map((pid, idx) => (
          <li key={pid}>
            <code>{pid}</code>
            {!known.has(pid) && <span className="warn"> (unknown)</span>}
            <button onClick={() => move(idx, -1)} disabled={idx === 0}>↑</button>
            <button onClick={() => move(idx, +1)} disabled={idx === aggIds.length - 1}>↓</button>
            <button onClick={() => remove(idx)}>Remove</button>
          </li>
        ))}
      </ol>
      {candidates.length > 0 && (
        <div>
          Add: {candidates.map((pid) => (
            <button key={pid} onClick={() => add(pid)}>+ {pid}</button>
          ))}
        </div>
      )}
      {aggIds.length === 0 && <div className="warn">At least one provider is required.</div>}
    </div>
  );
}


window.WebSearchPage = WebSearchPage;
