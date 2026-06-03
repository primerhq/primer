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
  const { useResource, apiFetch } = window.primerApi;

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
        onDelete={(_row) => { /* see Task 8.3 */ }}
        onTest={(_row) => { /* see Task 8.3 */ }}
        pushToast={pushToast}
      />

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
  return <div className="modal"><button onClick={onClose}>Close</button></div>;
}


window.WebSearchPage = WebSearchPage;
