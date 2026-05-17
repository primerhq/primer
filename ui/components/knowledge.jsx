/* global React, Icon, Btn, StatusPill, Modal, Banner, relativeTime */

const { apiFetch, useResource, useMutation, useRouter, useToast } = window.matrixApi;

// 404 → null suppression for IC config (same pattern as chrome / dashboard).
async function _fetchIcConfig(signal) {
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

function CollectionsPage() {
  const { navigate } = useRouter();
  const { push: pushToast } = useToast();

  const list = useResource("collections:list",
    (s) => apiFetch("GET", "/collections?limit=200", null, { signal: s }),
    { pollMs: null });
  const embedProviders = useResource("collections:embedding-providers",
    (s) => apiFetch("GET", "/embedding_providers?limit=200", null, { signal: s }),
    { pollMs: null });

  const [selected, setSelected] = React.useState(null);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [textFilter, setTextFilter] = React.useState("");

  const items = list.data?.items ?? [];
  const filtered = items.filter((c) => !textFilter || c.id.toLowerCase().includes(textFilter.toLowerCase()));
  const sel = selected ? items.find((c) => c.id === selected) : null;

  return (
    <div className="col" style={{ gap: 14 }}>
      <CollectionsHeader count={items.length} onRefresh={list.refetch} onNew={() => setCreateOpen(true)} />

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter collections…" value={textFilter} onChange={(e) => setTextFilter(e.target.value)} />
        </div>
        <div style={{ marginLeft: "auto" }}>
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
                  <tr><td colSpan={4}><EmptyState ico="collection" head="No collections yet" sub="A collection groups documents for similarity search; each is bound to one embedding provider + model." cta={<Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New collection</Btn>} /></td></tr>
                ) : (
                  <tr><td colSpan={4} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No collections match "{textFilter}".</td></tr>
                )
              ) : filtered.map((c) => (
                <tr key={c.id} className={selected === c.id ? "selected" : ""} onClick={() => setSelected(selected === c.id ? null : c.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{c.id}</td>
                  <td className="muted text-sm">{c.description || <span style={{ color: "var(--text-4)" }}>—</span>}</td>
                  <td className="mono muted text-sm">{c.embedder?.provider_id || "—"}</td>
                  <td className="mono muted text-sm">{c.embedder?.model || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {sel && <CollectionDetail c={sel} navigate={navigate} />}
      </div>

      {createOpen && (
        <NewCollectionModal
          embedProviders={embedProviders.data?.items ?? []}
          onClose={() => setCreateOpen(false)}
          onCreate={(c) => {
            setCreateOpen(false);
            pushToast({ kind: "success", title: "Collection created", detail: c.id });
            list.refetch();
          }}
        />
      )}
    </div>
  );
}

function CollectionsHeader({ count, onRefresh, onNew }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span>Knowledge</span><span className="sep">/</span><span style={{ color: "var(--text)" }}>Collections</span>
        </div>
        <h1 className="page-title">Collections</h1>
        <div className="page-sub tabular">{count} collection{count === 1 ? "" : "s"} <span className="muted">· manual refresh</span></div>
      </div>
      <div className="page-actions">
        <Btn icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>
        <Btn icon="plus" kind="primary" onClick={onNew}>New collection</Btn>
      </div>
    </div>
  );
}

function CollectionDetail({ c, navigate }) {
  const docs = useResource(`collection-docs-count:${c.id}`,
    (s) => apiFetch("GET", `/collections/${encodeURIComponent(c.id)}/documents?limit=1`, null, { signal: s }),
    { pollMs: null, deps: [c.id] });
  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name="collection" size={13} className="muted" />
        <span className="mono">{c.id}</span>
        {c.system && <span className="pill" style={{ marginLeft: 8 }}><span className="dot"></span>system</span>}
      </div>
      <div className="panel-body">
        <div className="kv" style={{ gridTemplateColumns: "120px 1fr" }}>
          <dt>description</dt><dd>{c.description || <span className="muted">—</span>}</dd>
          <dt>embedding</dt><dd className="mono">{c.embedder?.provider_id || "—"}</dd>
          <dt>model</dt><dd className="mono">{c.embedder?.model || "—"}</dd>
          <dt>docs</dt><dd className="mono num tabular">{docs.data?.total ?? "—"}</dd>
        </div>
        <div className="mt-3" style={{ display: "flex", gap: 6 }}>
          <Btn size="sm" kind="primary" icon="doc" onClick={() => navigate("/knowledge/documents", { collection: c.id })}>View documents</Btn>
          <Btn size="sm" kind="ghost" icon="search" onClick={() => navigate("/knowledge/search", { target: "collections", collection_id: c.id })}>Test search</Btn>
        </div>
      </div>
    </div>
  );
}

function NewCollectionModal({ embedProviders, onClose, onCreate }) {
  const { push: pushToast } = useToast();
  const [id, setId] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [providerId, setProviderId] = React.useState("");
  const [model, setModel] = React.useState("");
  const [fieldErrors, setFieldErrors] = React.useState({});

  React.useEffect(() => {
    if (!providerId && embedProviders.length > 0) setProviderId(embedProviders[0].id);
  }, [embedProviders, providerId]);

  // Models come from the selected provider's row.
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
      description: description || null,
      embedder: { provider_id: providerId, model },
    };
    try { await create.mutate(body); } catch (_e) {}
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
        <input className="input" value={id} onChange={(e) => setId(e.target.value)} placeholder="auto-generated" style={{ width: "100%" }} />
      </div>
      <div className="field">
        <label className="field-label">Description</label>
        <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="optional" style={{ width: "100%" }} />
      </div>
      <div className="field">
        <label className="field-label">Embedding provider</label>
        <select className="select" value={providerId} onChange={(e) => setProviderId(e.target.value)} style={{ width: "100%" }}>
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
        <select className="select" value={model} onChange={(e) => setModel(e.target.value)} style={{ width: "100%" }}>
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

function DocumentsPage() {
  const { query, navigate } = useRouter();
  const { push: pushToast } = useToast();
  const collectionFilter = query.collection || "";

  const list = useResource(`documents:list:${collectionFilter}`,
    (s) => apiFetch("GET",
      collectionFilter
        ? `/collections/${encodeURIComponent(collectionFilter)}/documents?limit=200`
        : "/documents?limit=200",
      null, { signal: s }),
    { pollMs: null, deps: [collectionFilter] });
  const collections = useResource("collections:list",
    (s) => apiFetch("GET", "/collections?limit=200", null, { signal: s }),
    { pollMs: null });

  const [textFilter, setTextFilter] = React.useState("");
  const [createOpen, setCreateOpen] = React.useState(false);

  const items = list.data?.items ?? [];
  const filtered = items.filter((d) => !textFilter || d.id.toLowerCase().includes(textFilter.toLowerCase()) || (d.name || "").toLowerCase().includes(textFilter.toLowerCase()));

  // For the orphan-document ⚠ chip (T0068): build a set of known collection ids.
  const knownCollections = new Set((collections.data?.items ?? []).map((c) => c.id));

  return (
    <div className="col" style={{ gap: 14 }}>
      <DocumentsHeader count={items.length} collectionFilter={collectionFilter} onRefresh={list.refetch} onNew={() => setCreateOpen(true)} />

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter documents…" value={textFilter} onChange={(e) => setTextFilter(e.target.value)} />
        </div>
        <div className="sep-v" />
        <select className="select" value={collectionFilter} onChange={(e) => navigate("/knowledge/documents", e.target.value ? { collection: e.target.value } : {})}>
          <option value="">all collections</option>
          {(collections.data?.items ?? []).map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
        </select>
        {collectionFilter && (
          <Btn size="sm" kind="ghost" icon="x" onClick={() => navigate("/knowledge/documents")}>Clear</Btn>
        )}
        <div style={{ marginLeft: "auto" }}>
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
                <tr><td colSpan={4}><EmptyState ico="doc" head="No documents yet" sub="Documents are ingested into a collection and indexed for similarity search." cta={<Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Ingest document</Btn>} /></td></tr>
              ) : (
                <tr><td colSpan={4} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No documents match.</td></tr>
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
        <NewDocumentModal
          collections={collections.data?.items ?? []}
          defaultCollection={collectionFilter}
          onClose={() => setCreateOpen(false)}
          onCreate={(d) => {
            setCreateOpen(false);
            pushToast({ kind: "success", title: "Document created", detail: d.id });
            list.refetch();
          }}
        />
      )}
    </div>
  );
}

function DocumentsHeader({ count, collectionFilter, onRefresh, onNew }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span>Knowledge</span><span className="sep">/</span><span style={{ color: "var(--text)" }}>Documents</span>
          {collectionFilter && <><span className="sep">/</span><span className="mono">{collectionFilter}</span></>}
        </div>
        <h1 className="page-title">Documents</h1>
        <div className="page-sub tabular">{count} document{count === 1 ? "" : "s"}</div>
      </div>
      <div className="page-actions">
        <Btn icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>
        <Btn icon="plus" kind="primary" onClick={onNew}>Ingest document</Btn>
      </div>
    </div>
  );
}

function NewDocumentModal({ collections, defaultCollection, onClose, onCreate }) {
  const { push: pushToast } = useToast();
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
    let meta = {};
    if (metaJson.trim()) {
      try { meta = JSON.parse(metaJson); }
      catch (e) { setFieldErrors({ "body.meta": "Invalid JSON: " + e.message }); return; }
    }
    // Document.id is REQUIRED by the backend (unlike most Identifiable
    // entities which auto-generate). Mint a short-form id if the
    // operator didn't supply one. Format: "doc-<8 hex>".
    const docId = "doc-" + Math.random().toString(16).slice(2, 10);
    const body = {
      id: docId,
      collection_id: collectionId,
      name: name || "(untitled)",
      meta,
    };
    // The text payload is application-defined per the model docs;
    // backend doesn't accept it on the row directly. v1 stores it in
    // meta.text so it's retrievable. (Document storage shape may
    // evolve — this is a pragmatic compromise.)
    if (text) body.meta = { ...meta, text };
    try { await create.mutate(body); } catch (_e) {}
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
        <select className="select" value={collectionId} onChange={(e) => setCollectionId(e.target.value)} style={{ width: "100%" }}>
          <option value="">-- pick a collection --</option>
          {collections.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
        </select>
        {fieldErrors["body.collection_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.collection_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Name</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} style={{ width: "100%" }} />
        {fieldErrors["body.name"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.name"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Text <span className="hint">stored under meta.text for v1</span></label>
        <textarea className="textarea" value={text} onChange={(e) => setText(e.target.value)} rows={4} />
      </div>
      <div className="field">
        <label className="field-label">Meta (JSON)</label>
        <textarea className="textarea mono" value={metaJson} onChange={(e) => setMetaJson(e.target.value)} rows={3} placeholder='{ "source": "manual" }' />
        {fieldErrors["body.meta"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.meta"]}</div>}
      </div>
    </Modal>
  );
}

// ============================================================================
// Search bench
// ============================================================================

const SEARCH_TARGETS = ["agents", "graphs", "collections", "tools"];

function SearchBench() {
  const { navigate } = useRouter();
  const { push: pushToast } = useToast();
  const ic = useResource("sidebar:ic-config", _fetchIcConfig, { pollMs: 30000 });
  const subsystemOn = ic.data != null;

  const [target, setTarget] = React.useState("collections");
  const [query, setQuery] = React.useState("");
  const [topK, setTopK] = React.useState(5);

  const [results, setResults] = React.useState(null);
  const [latencyMs, setLatencyMs] = React.useState(null);

  const search = useMutation(
    async (body) => {
      const t0 = performance.now();
      const result = await apiFetch("POST", "/" + target + "/search", body);
      const wallMs = performance.now() - t0;
      return { result, wallMs };
    },
    {
      onSuccess: ({ result, wallMs }) => {
        setResults(result.hits || []);
        setLatencyMs(Math.round(wallMs));
      },
      onError: (err) => {
        setResults(null);
        if (err.status === 503) {
          pushToast({ kind: "warning", title: "Subsystem inactive", detail: "Bootstrap Internal Collections to enable search.", requestId: err.requestId });
        } else {
          pushToast({ kind: "error", title: err.title || "Search failed", detail: err.detail || err.message, requestId: err.requestId });
        }
      },
    }
  );

  const run = () => {
    if (!query.trim()) return;
    search.mutate({ query, top_k: topK });
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <SearchHeader />

      {!subsystemOn && (
        <Banner
          kind="error"
          title="Internal Collections subsystem is OFF"
          detail="All /search routes return 503 until the subsystem is configured and bootstrapped."
          actions={<Btn size="sm" kind="primary" icon="settings" onClick={() => navigate("/subsystems/internal-collections")}>Configure</Btn>}
        />
      )}

      <div className="panel">
        <div className="panel-h">
          <Icon name="search" size={13} className="muted" />
          <span>Query</span>
          <span className="sub">· POST /v1/{target}/search</span>
          <div className="right">
            <span className="mono text-sm" style={{ color: subsystemOn ? "var(--green)" : "var(--text-3)" }}>
              ● {subsystemOn ? "subsystem ON" : "subsystem OFF"}
            </span>
          </div>
        </div>
        <div className="panel-body">
          <div className="chip-group" style={{ marginBottom: 10 }}>
            {SEARCH_TARGETS.map((t) => (
              <span key={t} className={`chip ${target === t ? "active" : ""}`} onClick={() => setTarget(t)} style={{ cursor: "pointer" }}>
                <Icon name={t === "agents" ? "agent" : t === "graphs" ? "graph" : t === "collections" ? "collection" : "tools"} size={11} />
                <span>/{t}/search</span>
              </span>
            ))}
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
            <textarea
              className="textarea"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              rows={2}
              style={{ flex: 1, fontFamily: "inherit", fontSize: 13 }}
              placeholder="Natural-language query…"
              onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") run(); }}
            />
            <Btn kind="primary" icon="search" disabled={!subsystemOn || !query.trim() || search.loading} onClick={run}>
              {search.loading ? "Searching…" : "Search"}
            </Btn>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 14, marginTop: 10, fontSize: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className="muted">top_k</span>
              <input className="input" type="number" value={topK} onChange={(e) => setTopK(Math.max(1, Math.min(50, +e.target.value)))} min={1} max={50} style={{ width: 60 }} />
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
              <span className="muted text-sm">{" "}(Ctrl/⌘+Enter)</span>
            </div>
          ) : results.length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--text-3)" }}>No matches.</div>
          ) : (
            <div>
              {results.map((r, i) => <SearchResult key={(r.document_id || r.chunk_id || i) + "-" + i} r={r} rank={i + 1} />)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SearchHeader() {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span>Knowledge</span><span className="sep">/</span><span style={{ color: "var(--text)" }}>Search bench</span>
        </div>
        <h1 className="page-title">Search test bench</h1>
        <div className="page-sub">Probe <span className="mono">POST /v1/{`{agents|graphs|collections|tools}`}/search</span></div>
      </div>
    </div>
  );
}

function SearchResult({ r, rank }) {
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
          <Highlight text={r.text} />
        </div>
      )}
      {open && r.meta && (
        <div style={{ paddingLeft: 34, marginTop: 8 }}>
          <div className="muted text-sm mono mb-2">meta:</div>
          <div className="code-block" dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(r.meta, null, 2)) }} />
        </div>
      )}
    </div>
  );
}

function Highlight({ text }) {
  const parts = String(text).split(/(\*\*[^*]+\*\*)/g);
  return parts.map((p, i) => p.startsWith("**") ? (
    <span key={i} style={{ background: "var(--accent-dim)", color: "var(--accent)", padding: "0 2px", borderRadius: 2 }}>{p.slice(2, -2)}</span>
  ) : <span key={i}>{p}</span>);
}

// ============================================================================
// Shared empty state
// ============================================================================

function EmptyState({ ico, head, sub, cta }) {
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
