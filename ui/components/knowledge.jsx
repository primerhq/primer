/* global React, Icon, Btn, StatusPill, Modal, Banner, relativeTime */

// Knowledge: Collections + Documents + SearchBench wired to the real API.
// The Designer's mock-data scaffold was replaced in Phase 2 — every fetch
// goes through window.matrixApi.{apiFetch, useResource, useMutation,
// useRouter}. Cache-key convention follows other components:
//
//   collections:list                — GET /collections?limit=200
//   collections:embedding-providers — GET /embedding_providers?limit=200
//   documents:list:${collectionId}  — GET /documents (filter by collection)
//   collection-docs-count:${id}     — per-collection doc count probe
//   knowledge:ic-config             — GET /internal_collections/config
//                                      (404 → OFF, mirrors toolsets.jsx)
//
// Babel-standalone shares the global scope across <script> tags so every
// top-level binding in this file is prefixed with KN_ to avoid name clashes
// with other components (TS_*, AG_*, WS_*, etc.).

const KN_SEARCH_TARGETS = ["agents", "graphs", "tools"];

function _knToastErr(pushToast, fallbackTitle) {
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

// 404 → null suppression for IC config (mirrors toolsets.jsx).
async function _knFetchIcConfig(signal) {
  const { apiFetch } = window.matrixApi;
  try {
    return await apiFetch("GET", "/internal_collections/config", null, { signal });
  } catch (err) {
    if (err && err.status === 404) return null;
    throw err;
  }
}

// ============================================================================
// Collections page
// ============================================================================

function CollectionsPage({ pushToast, onOpen, onSearchCollection, onNavigate }) {
  const { useResource, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();

  const list = useResource(
    "collections:list",
    (signal) => apiFetch("GET", "/collections?limit=200", null, { signal }),
    { pollMs: null },
  );
  const embedProviders = useResource(
    "collections:embedding-providers",
    (signal) => apiFetch("GET", "/embedding_providers?limit=200", null, { signal }),
    { pollMs: null },
  );

  const [selected, setSelected] = React.useState(null);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [textFilter, setTextFilter] = React.useState("");

  const items = list.data?.items ?? [];
  const filtered = items.filter(
    (c) => !textFilter || (c.id || "").toLowerCase().includes(textFilter.toLowerCase()),
  );
  const sel = selected ? items.find((c) => c.id === selected) : null;

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter collections…"
            value={textFilter}
            onChange={(e) => setTextFilter(e.target.value)}
          />
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New collection</Btn>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: sel ? "1.6fr 1fr" : "1fr", gap: 18 }}>
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>ID</th>
                <th>Description</th>
                <th>Embedding provider</th>
                <th>Model</th>
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
                    <KN_EmptyState
                      ico="collection"
                      head="No collections yet"
                      sub="A collection groups documents for similarity search; each is bound to one embedding provider + model."
                      cta={<Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New collection</Btn>}
                    />
                  </td></tr>
                ) : (
                  <tr><td colSpan={4} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                    No collections match "{textFilter}".
                  </td></tr>
                )
              ) : filtered.map((c) => (
                <tr
                  key={c.id}
                  className={selected === c.id ? "selected" : ""}
                  onClick={() => setSelected(selected === c.id ? null : c.id)}
                  style={{ cursor: "pointer" }}
                >
                  <td className="mono">{c.id}</td>
                  <td className="muted text-sm">
                    {c.description || <span style={{ color: "var(--text-4)" }}>—</span>}
                  </td>
                  <td className="mono muted text-sm">{c.embedder?.provider_id || "—"}</td>
                  <td className="mono muted text-sm">{c.embedder?.model || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {sel && (
          <KN_CollectionDetail
            c={sel}
            pushToast={pushToast}
            onOpenDocs={() => {
              if (typeof onOpen === "function") onOpen(sel.id);
              else navigate("/knowledge/documents", { collection: sel.id });
            }}
            onSearchCollection={onSearchCollection}
            onNavigate={onNavigate}
          />
        )}
      </div>

      {createOpen && (
        <KN_NewCollectionModal
          embedProviders={embedProviders.data?.items ?? []}
          pushToast={pushToast}
          onClose={() => setCreateOpen(false)}
          onCreate={(c) => {
            setCreateOpen(false);
            if (typeof pushToast === "function") {
              pushToast({ kind: "success", title: "Collection created", detail: c.id });
            }
            list.refetch();
          }}
        />
      )}
    </div>
  );
}

function KN_CollectionDetail({ c, pushToast, onOpenDocs, onSearchCollection, onNavigate }) {
  const { useResource, apiFetch } = window.matrixApi;
  const docs = useResource(
    `collection-docs-count:${c.id}`,
    (signal) => apiFetch(
      "GET",
      `/collections/${encodeURIComponent(c.id)}/documents?limit=1`,
      null,
      { signal },
    ),
    { pollMs: null, deps: [c.id] },
  );
  // System (internal) collections store their content directly in the
  // vector index — there are no Document rows backing them. The docs
  // count + Documents page reflect storage rows only, so they'd both be
  // misleadingly empty. Suppress the count and the "View documents"
  // button for system collections; the inline search panel below
  // exposes both query and a "Browse all" mode against the vector store.
  const isSystem = !!c.system;
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div className="panel-h">
          <Icon name="collection" size={13} className="muted" />
          <span className="mono">{c.id}</span>
          {isSystem && <span className="pill" style={{ marginLeft: 8 }}><span className="dot"></span>system</span>}
        </div>
        <div className="panel-body">
          <div className="kv" style={{ gridTemplateColumns: "120px 1fr" }}>
            <dt>description</dt><dd>{c.description || <span className="muted">—</span>}</dd>
            <dt>embedding</dt><dd className="mono">{c.embedder?.provider_id || "—"}</dd>
            <dt>model</dt><dd className="mono">{c.embedder?.model || "—"}</dd>
            <dt>docs</dt>
            <dd className="mono num tabular">
              {isSystem
                ? <span className="muted">vector-only · search below</span>
                : (docs.data?.total ?? "—")}
            </dd>
          </div>
          {!isSystem && (
            <div className="mt-3" style={{ display: "flex", gap: 6 }}>
              <Btn size="sm" kind="primary" icon="doc" onClick={onOpenDocs}>View documents</Btn>
            </div>
          )}
        </div>
      </div>
      <KN_CollectionSearchPanel collection={c} pushToast={pushToast} />
    </div>
  );
}

function KN_CollectionSearchPanel({ collection, pushToast }) {
  const { useMutation, apiFetch } = window.matrixApi;
  const [query, setQuery] = React.useState("");
  const [topK, setTopK] = React.useState(10);
  const [hits, setHits] = React.useState(null);
  const [latencyMs, setLatencyMs] = React.useState(null);
  const [browseTruncated, setBrowseTruncated] = React.useState(false);
  const [mode, setMode] = React.useState("search"); // "search" | "browse"

  const search = useMutation(
    async (body) => {
      const t0 = performance.now();
      const result = await apiFetch(
        "POST",
        `/collections/${encodeURIComponent(collection.id)}/search`,
        body,
      );
      return { result, wallMs: Math.round(performance.now() - t0) };
    },
    {
      onSuccess: ({ result, wallMs }) => {
        setHits(result.hits || []);
        setBrowseTruncated(false);
        setLatencyMs(wallMs);
        setMode("search");
      },
      onError: (err) => {
        setHits(null);
        setLatencyMs(null);
        if (typeof pushToast !== "function") return;
        if (err?.status === 404) {
          pushToast({ kind: "error", title: "Collection not found", detail: collection.id, requestId: err.requestId });
        } else {
          pushToast({ kind: "error", title: err?.title || "Search failed", detail: err?.detail || err?.message, requestId: err?.requestId });
        }
      },
    },
  );

  const browse = useMutation(
    async () => {
      const t0 = performance.now();
      const result = await apiFetch(
        "GET",
        `/collections/${encodeURIComponent(collection.id)}/indexed_documents?limit=200`,
      );
      return { result, wallMs: Math.round(performance.now() - t0) };
    },
    {
      onSuccess: ({ result, wallMs }) => {
        // Normalise to the same hit shape the search path uses so the
        // renderer below doesn't need a second code path.
        const items = (result.items || []).map((r) => ({
          document_id: r.document_id,
          chunk_id: r.chunk_id,
          text: r.text,
          meta: r.meta,
          score: null,
        }));
        setHits(items);
        setBrowseTruncated(!!result.truncated);
        setLatencyMs(wallMs);
        setMode("browse");
      },
      onError: (err) => {
        if (typeof pushToast !== "function") return;
        pushToast({
          kind: "error",
          title: err?.title || "Browse failed",
          detail: err?.detail || err?.message,
          requestId: err?.requestId,
        });
      },
    },
  );

  const run = () => {
    if (!query.trim()) return;
    search.mutate({ query, top_k: topK });
  };

  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name="search" size={13} className="muted" />
        <span>Search this collection</span>
        <span className="sub muted">· <span className="mono">POST /v1/collections/{collection.id}/search</span></span>
      </div>
      <div className="panel-body">
        <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
          <textarea
            className="textarea"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            rows={2}
            style={{ flex: 1, fontFamily: "inherit", fontSize: 13 }}
            placeholder="Natural-language query… (Enter to run · Shift+Enter for newline)"
            onKeyDown={(e) => {
              // Enter submits; Shift+Enter inserts newline. Ctrl/Cmd+Enter
              // also submits for parity with the previous build.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                run();
              }
            }}
          />
          <Btn kind="primary" icon="search" disabled={!query.trim() || search.loading || browse.loading} onClick={run}>
            {search.loading ? "Searching…" : "Search"}
          </Btn>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 14, marginTop: 10, fontSize: 12, flexWrap: "wrap" }}>
          <label className="muted" htmlFor={`kn-topk-${collection.id}`}>top_k</label>
          <input
            id={`kn-topk-${collection.id}`}
            className="input"
            type="number"
            min="1"
            max="100"
            value={topK}
            onChange={(e) => setTopK(Number(e.target.value) || 1)}
            style={{ width: 70 }}
          />
          <Btn
            size="sm"
            kind="ghost"
            icon="doc"
            disabled={search.loading || browse.loading}
            onClick={() => browse.mutate()}
            title="List every entry the vector store has for this collection — useful for system collections that have no Document rows."
          >
            {browse.loading ? "Loading…" : "Browse all entries"}
          </Btn>
          {latencyMs != null && (
            <span className="muted tabular">· {latencyMs} ms · {hits?.length ?? 0} {mode === "browse" ? "entries" : (hits?.length === 1 ? "hit" : "hits")}{browseTruncated ? " (truncated)" : ""}</span>
          )}
        </div>

        {hits != null && hits.length === 0 && (
          <div className="muted text-sm mt-3">
            {mode === "browse"
              ? "No indexed entries. Bootstrap Internal Collections (for system collections) or ingest documents."
              : "No matches. Either the collection is empty, or the query doesn't match any indexed chunk well enough."}
          </div>
        )}
        {hits != null && hits.length > 0 && (
          <div className="mt-3" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {hits.map((h, i) => (
              <div key={i} style={{ borderTop: "1px solid var(--border)", padding: "8px 0" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11 }}>
                  <span className="mono num tabular muted">#{i + 1}</span>
                  <span className="mono">{h.document_id}</span>
                  <span className="muted">·</span>
                  <span className="mono muted">{h.chunk_id}</span>
                  {h.score != null && (
                    <span className="muted" style={{ marginLeft: "auto" }}>score {Number(h.score).toFixed(4)}</span>
                  )}
                </div>
                <div className="text-sm mt-1" style={{ whiteSpace: "pre-wrap", lineHeight: 1.45 }}>
                  {h.text}
                </div>
                {h.meta && Object.keys(h.meta).length > 0 && (
                  <div className="muted text-sm mt-1 mono" style={{ fontSize: 10.5 }}>
                    {Object.entries(h.meta).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" · ")}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function KN_NewCollectionModal({ embedProviders, pushToast, onClose, onCreate }) {
  const { useMutation, apiFetch } = window.matrixApi;
  const [id, setId] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [providerId, setProviderId] = React.useState("");
  const [model, setModel] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState({});

  React.useEffect(() => {
    if (!providerId && embedProviders.length > 0) setProviderId(embedProviders[0].id);
  }, [embedProviders, providerId]);

  // Model options come from the selected provider's row (T0025 — no live
  // introspection; the provider stores its declared model list).
  const selectedProvider = embedProviders.find((p) => p.id === providerId);
  const modelOptions = selectedProvider?.models ?? [];
  React.useEffect(() => {
    if (modelOptions.length > 0 && !modelOptions.some((m) => m.name === model)) {
      setModel(modelOptions[0].name);
    }
  }, [modelOptions]);  // eslint-disable-line react-hooks/exhaustive-deps

  const create = useMutation(
    (body) => apiFetch("POST", "/collections", body),
    {
      invalidates: ["collections:list"],
      onSuccess: (c) => onCreate(c),
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
            title: err?.title || "Create failed",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    },
  );

  const submit = async () => {
    setFieldErrors({});
    const body = {
      ...(id ? { id } : {}),
      description: description || null,
      embedder: { provider_id: providerId, model },
    };
    try { await create.mutate(body); } catch (_e) { /* surfaced via onError */ }
  };

  return (
    <Modal
      title="New collection"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit} disabled={!providerId || !model || create.loading}>
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">ID <span className="hint">optional — backend assigns if blank</span></label>
        <input
          className="input"
          value={id}
          onChange={(e) => setId(e.target.value)}
          placeholder="auto-generated"
          style={{ width: "100%" }}
        />
        {fieldErrors["body.id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Description</label>
        <input
          className="input"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="optional"
          style={{ width: "100%" }}
        />
      </div>
      <div className="field">
        <label className="field-label">Embedding provider</label>
        <select
          className="select"
          value={providerId}
          onChange={(e) => setProviderId(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="">-- pick a provider --</option>
          {embedProviders.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        {embedProviders.length === 0 && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No embedding providers configured. Create one at /providers/embedding first.
          </div>
        )}
        {fieldErrors["body.embedder.provider_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.embedder.provider_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Model</label>
        <select
          className="select"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="">-- pick a model --</option>
          {modelOptions.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
        </select>
        <div className="field-help">Model list comes from the provider row, not a live introspection (T0025).</div>
        {fieldErrors["body.embedder.model"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.embedder.model"]}</div>}
      </div>
    </Modal>
  );
}

// ============================================================================
// Documents page
// ============================================================================

function DocumentsPage({ pushToast, filterCollection, onClearFilter }) {
  const { useResource, useRouter, apiFetch } = window.matrixApi;
  const { query, navigate } = useRouter();
  // Prefer the explicit prop (app.jsx passes docsFilterCollection) and fall
  // back to the router query for deep-link cases.
  const collectionFilter = (filterCollection != null && filterCollection !== "")
    ? filterCollection
    : (query.collection || "");

  const list = useResource(
    `documents:list:${collectionFilter}`,
    (signal) => apiFetch(
      "GET",
      collectionFilter
        ? `/collections/${encodeURIComponent(collectionFilter)}/documents?limit=200`
        : "/documents?limit=200",
      null,
      { signal },
    ),
    { pollMs: null, deps: [collectionFilter] },
  );
  const collections = useResource(
    "collections:list",
    (signal) => apiFetch("GET", "/collections?limit=200", null, { signal }),
    { pollMs: null },
  );

  const [textFilter, setTextFilter] = React.useState("");
  const [createOpen, setCreateOpen] = React.useState(false);

  const items = list.data?.items ?? [];
  const filtered = items.filter(
    (d) => !textFilter
      || (d.id || "").toLowerCase().includes(textFilter.toLowerCase())
      || (d.name || "").toLowerCase().includes(textFilter.toLowerCase()),
  );

  // T0068 — POST /documents doesn't enforce referential integrity at
  // create-time. Surface a ⚠ chip on orphan rows by joining against
  // /v1/collections.
  const knownCollections = new Set((collections.data?.items ?? []).map((c) => c.id));

  const setCollectionFilter = (next) => {
    // Drop the local state in app.jsx (if it's tracking us) and use the
    // router for state.
    if (typeof onClearFilter === "function" && !next) onClearFilter();
    navigate("/knowledge/documents", next ? { collection: next } : {});
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input
            className="input"
            placeholder="Filter documents…"
            value={textFilter}
            onChange={(e) => setTextFilter(e.target.value)}
          />
        </div>
        <div className="sep-v" />
        <select
          className="select"
          value={collectionFilter}
          onChange={(e) => setCollectionFilter(e.target.value)}
        >
          <option value="">all collections</option>
          {(collections.data?.items ?? []).map((c) => (
            <option key={c.id} value={c.id}>{c.id}</option>
          ))}
        </select>
        {collectionFilter && (
          <Btn size="sm" kind="ghost" icon="x" onClick={() => setCollectionFilter("")}>Clear</Btn>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Ingest document</Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Collection</th>
              <th>Name</th>
              <th>Meta keys</th>
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
                  <KN_EmptyState
                    ico="doc"
                    head="No documents yet"
                    sub="Documents are ingested into a collection and indexed for similarity search."
                    cta={<Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Ingest document</Btn>}
                  />
                </td></tr>
              ) : (
                <tr><td colSpan={4} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
                  No documents match.
                </td></tr>
              )
            ) : filtered.map((d) => {
              const orphan = d.collection_id && !knownCollections.has(d.collection_id);
              return (
                <tr key={d.id}>
                  <td className="mono">{d.id}</td>
                  <td className="mono muted text-sm">
                    {d.collection_id}
                    {orphan && (
                      <span
                        title="Collection id not present in /v1/collections — known anomaly T0068: POST /documents does not enforce referential integrity at create-time"
                        style={{ marginLeft: 6, color: "var(--amber)" }}
                      >⚠</span>
                    )}
                  </td>
                  <td className="mono">{d.name}</td>
                  <td className="mono muted text-sm">
                    {Object.keys(d.meta || {}).length === 0
                      ? <span style={{ color: "var(--text-4)" }}>—</span>
                      : Object.keys(d.meta).join(", ")}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <KN_NewDocumentModal
          collections={collections.data?.items ?? []}
          defaultCollection={collectionFilter}
          pushToast={pushToast}
          onClose={() => setCreateOpen(false)}
          onCreate={(d) => {
            setCreateOpen(false);
            if (typeof pushToast === "function") {
              pushToast({ kind: "success", title: "Document created", detail: d.id });
            }
            list.refetch();
          }}
        />
      )}
    </div>
  );
}

function KN_NewDocumentModal({ collections, defaultCollection, pushToast, onClose, onCreate }) {
  const { useMutation, apiFetch } = window.matrixApi;
  const [collectionId, setCollectionId] = React.useState(defaultCollection || "");
  const [name, setName] = React.useState("");
  const [text, setText] = React.useState("");
  const [metaJson, setMetaJson] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState({});

  React.useEffect(() => {
    if (!collectionId && collections.length > 0) setCollectionId(collections[0].id);
  }, [collections, collectionId]);

  const create = useMutation(
    (body) => apiFetch("POST", "/documents", body),
    {
      onSuccess: (d) => onCreate(d),
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
            title: err?.title || "Create failed",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    },
  );

  const submit = async () => {
    setFieldErrors({});
    let meta = {};
    if (metaJson.trim()) {
      try { meta = JSON.parse(metaJson); }
      catch (e) { setFieldErrors({ "body.meta": "Invalid JSON: " + e.message }); return; }
    }
    // Document.id is required by the backend (unlike most Identifiable
    // entities which auto-generate). Mint a short-form id if blank.
    const docId = "doc-" + Math.random().toString(16).slice(2, 10);
    const body = {
      id: docId,
      collection_id: collectionId,
      name: name || "(untitled)",
      meta,
    };
    // Text payload is application-defined; v1 stores it in meta.text.
    if (text) body.meta = { ...meta, text };
    try { await create.mutate(body); } catch (_e) { /* surfaced via onError */ }
  };

  return (
    <Modal
      title="Ingest document"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit} disabled={!collectionId || create.loading}>
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">Collection</label>
        <select
          className="select"
          value={collectionId}
          onChange={(e) => setCollectionId(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="">-- pick a collection --</option>
          {collections.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
        </select>
        {fieldErrors["body.collection_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.collection_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Name</label>
        <input
          className="input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          style={{ width: "100%" }}
        />
        {fieldErrors["body.name"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.name"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Text <span className="hint">stored under meta.text for v1</span></label>
        <textarea
          className="textarea"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={4}
        />
      </div>
      <div className="field">
        <label className="field-label">Meta (JSON)</label>
        <textarea
          className="textarea mono"
          value={metaJson}
          onChange={(e) => setMetaJson(e.target.value)}
          rows={3}
          placeholder='{ "source": "manual" }'
        />
        {fieldErrors["body.meta"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.meta"]}</div>}
      </div>
    </Modal>
  );
}

// ============================================================================
// Search bench — entity search probe across agents/graphs/tools
// ============================================================================
//
// Per-collection search lives inline on the Collection detail panel
// (KN_CollectionSearchPanel). What lives here is the "find entries by
// description" probe — same shape as agents/graphs/tools — exposed via
// the renamed "Entity search probe" sidebar entry.
//
// `subsystemOn` may be passed as a prop (from app.jsx tweaks) but we also
// probe the real /v1/internal_collections/config so the banner reflects
// the live API state when no prop is provided.

function SearchBench({ subsystemOn: subsystemOnProp, collectionId }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.matrixApi;
  const { navigate } = useRouter();

  // Source the toast push from the matrixApi namespace (toast.js publishes
  // window.matrixApi.toastPush) so this works whether or not app.jsx forwards
  // a pushToast prop into SearchBench.
  const pushToast = window.matrixApi?.toastPush || (() => {});

  const ic = useResource("knowledge:ic-config", _knFetchIcConfig, { pollMs: 30000 });
  // subsystemOn: prefer the live IC probe when it has resolved; fall back to
  // the app-level tweak. ic.data === null after the 404 path means OFF;
  // ic.data being an object means ON.
  const liveOn = ic.error == null ? (ic.data != null) : false;
  const subsystemOn = subsystemOnProp != null ? !!subsystemOnProp : liveOn;

  const [target, setTarget] = React.useState(KN_SEARCH_TARGETS[0]);
  const [query, setQuery] = React.useState("");
  const [topK, setTopK] = React.useState(5);

  const [results, setResults] = React.useState(null);
  const [latencyMs, setLatencyMs] = React.useState(null);

  // Scoped mode: caller pinned a specific collection. We always POST to
  // /collections/{id}/search in that case (target picker is hidden).
  const isScoped = !!collectionId;

  const search = useMutation(
    async (body) => {
      const t0 = performance.now();
      const path = isScoped
        ? `/collections/${encodeURIComponent(collectionId)}/search`
        : `/${target}/search`;
      const result = await apiFetch("POST", path, body);
      return { result, wallMs: performance.now() - t0 };
    },
    {
      onSuccess: ({ result, wallMs }) => {
        setResults(result.hits || []);
        setLatencyMs(Math.round(wallMs));
      },
      onError: (err) => {
        setResults(null);
        if (err?.status === 503) {
          pushToast({
            kind: "warning",
            title: "Subsystem inactive",
            detail: "Bootstrap Internal Collections to enable search.",
            requestId: err.requestId,
          });
        } else {
          pushToast({
            kind: "error",
            title: err?.title || "Search failed",
            detail: err?.detail || err?.message,
            requestId: err?.requestId,
          });
        }
      },
    },
  );

  const run = () => {
    if (!query.trim()) return;
    search.mutate({ query, top_k: topK });
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      {!subsystemOn && (
        <Banner
          kind="error"
          title="Internal Collections subsystem is OFF"
          detail="All /search routes return 503 until the subsystem is configured and bootstrapped."
          actions={
            <Btn
              size="sm"
              kind="primary"
              icon="settings"
              onClick={() => navigate("/subsystems/internal-collections")}
            >Configure</Btn>
          }
        />
      )}

      <div className="panel">
        <div className="panel-h">
          <Icon name="search" size={13} className="muted" />
          <span>Query</span>
          <span className="sub">· POST /v1/{isScoped ? `collections/${collectionId}` : target}/search</span>
          <div className="right">
            <span className="mono text-sm" style={{ color: subsystemOn ? "var(--green)" : "var(--text-3)" }}>
              ● {subsystemOn ? "subsystem ON" : "subsystem OFF"}
            </span>
          </div>
        </div>
        <div className="panel-body">
          {!isScoped && (
            <div className="chip-group" style={{ marginBottom: 10 }}>
              {KN_SEARCH_TARGETS.map((t) => (
                <span
                  key={t}
                  className={`chip ${target === t ? "active" : ""}`}
                  onClick={() => setTarget(t)}
                  style={{ cursor: "pointer" }}
                >
                  <Icon name={t === "agents" ? "agent" : t === "graphs" ? "graph" : "tools"} size={11} />
                  <span>/{t}/search</span>
                </span>
              ))}
            </div>
          )}
          <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
            <textarea
              className="textarea"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              rows={2}
              style={{ flex: 1, fontFamily: "inherit", fontSize: 13 }}
              placeholder="Natural-language query… (Enter to run · Shift+Enter for newline)"
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  run();
                }
              }}
            />
            <Btn
              kind="primary"
              icon="search"
              disabled={!subsystemOn || !query.trim() || search.loading}
              onClick={run}
            >
              {search.loading ? "Searching…" : "Search"}
            </Btn>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 14, marginTop: 10, fontSize: 12 }}>
            {isScoped && (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="muted">collection</span>
                <span className="mono" style={{ color: "var(--text)" }}>{collectionId}</span>
              </div>
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className="muted">top_k</span>
              <input
                className="input"
                type="number"
                value={topK}
                onChange={(e) => setTopK(Math.max(1, Math.min(50, +e.target.value)))}
                min={1}
                max={50}
                style={{ width: 60 }}
              />
            </div>
            <span className="muted text-sm">Backend silently ignores extra `filter`/`mmr` body fields (T0174).</span>
          </div>
        </div>
      </div>

      {/* Results */}
      <div className="panel">
        <div className="panel-h">
          <Icon name="list" size={13} className="muted" />
          <span>Results</span>
          {results && (
            <>
              <span className="sub">· {results.length} hit{results.length === 1 ? "" : "s"}</span>
              {latencyMs != null && <span className="sub">· <span className="mono" style={{ color: "var(--accent)" }}>{latencyMs}ms</span></span>}
            </>
          )}
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {search.loading ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--text-3)" }}>
              <Icon name="zap" size={18} style={{ color: "var(--accent)" }} />
              <div className="mt-2">Embedding query & running vector search…</div>
            </div>
          ) : results == null ? (
            <div style={{ padding: 36, textAlign: "center", color: "var(--text-4)", fontSize: 13 }}>
              Enter a query above and click <kbd style={{ background: "var(--bg-2)", border: "1px solid var(--border)", padding: "1px 5px", borderRadius: 4, fontFamily: "IBM Plex Mono" }}>Search</kbd>
              <span className="muted text-sm">{" "}(Enter)</span>
            </div>
          ) : results.length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--text-3)" }}>No matches.</div>
          ) : (
            <div>
              {results.map((r, i) => (
                <KN_SearchResult key={(r.document_id || r.chunk_id || i) + "-" + i} r={r} rank={i + 1} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function KN_SearchResult({ r, rank }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div style={{ borderBottom: "1px solid var(--border)", padding: "10px 14px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ width: 24, textAlign: "center", color: "var(--text-3)" }} className="mono num tabular">{rank}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="mono" style={{ fontSize: 12.5, fontWeight: 500 }}>{r.document_id || r.chunk_id || "(unnamed)"}</div>
          <div className="muted text-sm mono" style={{ fontSize: 11.5 }}>
            {r.chunk_id && r.document_id ? "chunk " + r.chunk_id : ""}
          </div>
        </div>
        {r.score != null && (
          <div>
            <div className="mono tabular" style={{ fontSize: 14, fontWeight: 600, color: "var(--accent)", textAlign: "right" }}>{Number(r.score).toFixed(3)}</div>
            <div className="muted text-sm mono" style={{ fontSize: 10.5, textAlign: "right" }}>score</div>
          </div>
        )}
        <button className="icon-btn" onClick={() => setOpen(!open)}>
          <Icon name={open ? "chevron-down" : "chevron-right"} size={11} />
        </button>
      </div>
      {r.text && (
        <div style={{ marginTop: 8, paddingLeft: 34, fontSize: 12, color: "var(--text-2)", lineHeight: 1.5 }}>
          <KN_Highlight text={r.text} />
        </div>
      )}
      {open && r.meta && (
        <div style={{ paddingLeft: 34, marginTop: 8 }}>
          <div className="muted text-sm mono mb-2">meta:</div>
          <div className="code-block">{JSON.stringify(r.meta, null, 2)}</div>
        </div>
      )}
    </div>
  );
}

function KN_Highlight({ text }) {
  // Highlight **bolded** terms — backend convention for query-matched
  // spans (when present in the snippet).
  const parts = String(text).split(/(\*\*[^*]+\*\*)/g);
  return parts.map((p, i) => p.startsWith("**") ? (
    <span key={i} style={{ background: "var(--accent-dim)", color: "var(--accent)", padding: "0 2px", borderRadius: 2 }}>{p.slice(2, -2)}</span>
  ) : <span key={i}>{p}</span>);
}

// ============================================================================
// Shared empty state
// ============================================================================

function KN_EmptyState({ ico, head, sub, cta }) {
  return (
    <div className="empty" style={{ padding: "40px 20px" }}>
      <div className="ico-wrap"><Icon name={ico} size={22} /></div>
      <div className="head">{head}</div>
      <div className="sub">{sub}</div>
      {cta && <div className="actions">{cta}</div>}
    </div>
  );
}

window.CollectionsPage = CollectionsPage;
window.DocumentsPage = DocumentsPage;
window.SearchBench = SearchBench;
