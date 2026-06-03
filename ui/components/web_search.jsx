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


function ProvidersTable({
  providers, loading, error, onCreate, onEdit, onDelete, onTest, pushToast,
}) {
  // Filled in in Task 8.2.
  return (
    <div className="card">
      <h2>Providers</h2>
      <button className="btn" onClick={onCreate}>+ Add provider</button>
      {/* Table body in Task 8.2 */}
    </div>
  );
}


// Stub modals — bodies in Task 8.2 / 8.3.
function ProviderEditModal({ row, onClose, onSaved, pushToast }) {
  return <div className="modal"><button onClick={onClose}>Close</button></div>;
}
function ActiveConfigModal({ active, providers, onClose, onSaved, pushToast }) {
  return <div className="modal"><button onClick={onClose}>Close</button></div>;
}


window.WebSearchPage = WebSearchPage;
