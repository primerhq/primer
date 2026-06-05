/* global React, Icon, Btn, StatusPill, Modal, Banner, CardList, Card, Fab, relativeTime */

// Knowledge: Collections + Documents + SearchBench wired to the real API.
// The Designer's mock-data scaffold was replaced in Phase 2 — every fetch
// goes through window.primerApi.{apiFetch, useResource, useMutation,
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
  const { apiFetch } = window.primerApi;
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
  const { useResource, useRouter, useViewport, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const { isMobile } = useViewport();

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
  const sspProviders = useResource(
    "collections:ssp",
    (signal) => apiFetch("GET", "/ssp?limit=200", null, { signal }),
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

      {isMobile ? (
        <CardList
          items={filtered}
          empty={items.length === 0 ? "No collections yet." : `No collections match "${textFilter}".`}
          renderCard={(c) => (
            <Card
              title={c.id}
              subtitle={c.description || ""}
              meta={`${c.embedder?.provider_id || "—"} · ${c.embedder?.model || "—"}`}
              pill={c.system ? <span className="pill"><span className="dot"></span>system</span> : null}
              onClick={() => {
                if (typeof onOpen === "function") onOpen(c.id);
                else navigate("/knowledge/documents", { collection: c.id });
              }}
            />
          )}
        />
      ) : (
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
            embedProviders={embedProviders.data?.items ?? []}
            sspProviders={sspProviders.data?.items ?? []}
            onOpenDocs={() => {
              if (typeof onOpen === "function") onOpen(sel.id);
              else navigate("/knowledge/documents", { collection: sel.id });
            }}
            onSearchCollection={onSearchCollection}
            onNavigate={onNavigate}
          />
        )}
      </div>
      )}

      {isMobile && (
        <Fab icon="plus" label="New collection" onClick={() => setCreateOpen(true)} />
      )}

      {createOpen && (
        <KN_NewCollectionModal
          embedProviders={embedProviders.data?.items ?? []}
          sspProviders={sspProviders.data?.items ?? []}
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

function KN_CollectionDetail({ c, pushToast, onOpenDocs, onSearchCollection, onNavigate, embedProviders, sspProviders }) {
  const { useResource, apiFetch } = window.primerApi;
  const isSystem = !!c.system;
  const isManaged = !!c.harness_id;
  const [listOpen, setListOpen] = React.useState(false);
  const [searchOpen, setSearchOpen] = React.useState(false);
  const [editOpen, setEditOpen] = React.useState(false);

  // For the docs count: system collections live in the vector store
  // only — Document storage rows are always empty, so probing them
  // would always render 0. Probe the vector-store enumeration instead.
  const storageDocs = useResource(
    `collection-docs-count:${c.id}`,
    (signal) => apiFetch(
      "GET",
      `/collections/${encodeURIComponent(c.id)}/documents?limit=1`,
      null,
      { signal },
    ),
    { pollMs: null, deps: [c.id, isSystem] },
  );
  const vectorDocs = useResource(
    `collection-indexed-count:${c.id}`,
    (signal) => isSystem
      ? apiFetch(
          "GET",
          `/collections/${encodeURIComponent(c.id)}/indexed_documents?limit=1`,
          null,
          { signal },
        )
      : Promise.resolve(null),
    { pollMs: null, deps: [c.id, isSystem] },
  );
  const docCount = isSystem
    ? (vectorDocs.data?.total ?? "—")
    : (storageDocs.data?.total ?? "—");

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
            <dt>docs</dt><dd className="mono num tabular">{docCount}</dd>
          </div>
          <div className="mt-3" style={{ display: "flex", gap: 6 }}>
            <Btn size="sm" kind="primary" icon="doc" onClick={() => setListOpen(true)}>List documents</Btn>
            <Btn size="sm" kind="ghost" icon="search" onClick={() => setSearchOpen(true)}>Search</Btn>
            {!isSystem && !isManaged && (
              <Btn size="sm" kind="secondary" icon="edit" onClick={() => setEditOpen(true)}>Edit</Btn>
            )}
          </div>
        </div>
      </div>

      {listOpen && (
        <KN_CollectionListModal
          collection={c}
          pushToast={pushToast}
          onClose={() => setListOpen(false)}
        />
      )}
      {searchOpen && (
        <KN_CollectionSearchModal
          collection={c}
          pushToast={pushToast}
          onClose={() => setSearchOpen(false)}
        />
      )}
      {editOpen && (
        <KN_NewCollectionModal
          existing={c}
          embedProviders={embedProviders || []}
          sspProviders={sspProviders || []}
          pushToast={pushToast}
          onClose={() => setEditOpen(false)}
          onCreate={() => {
            setEditOpen(false);
            if (typeof pushToast === "function") {
              pushToast({ kind: "info", title: "Collection updated", detail: c.id });
            }
          }}
        />
      )}
    </div>
  );
}

// Shared row renderer used by both modals — search hits and indexed
// entries are the same shape after normalisation (document_id, chunk_id,
// text, meta, optional score). Long ids / JSON-stringified meta would
// otherwise break out of the modal; wordBreak: break-word + min-width:0
// forces wrapping inside a flex/grid parent.
function KN_EntryRow({ entry, index }) {
  return (
    <div style={{
      borderTop: "1px solid var(--border)",
      padding: "8px 0",
      minWidth: 0,
      maxWidth: "100%",
      overflowWrap: "anywhere",
      wordBreak: "break-word",
    }}>
      <div style={{
        display: "flex",
        alignItems: "baseline",
        gap: 8,
        fontSize: 11,
        flexWrap: "wrap",
        minWidth: 0,
      }}>
        <span className="mono num tabular muted">#{index + 1}</span>
        <span className="mono" style={{ overflowWrap: "anywhere", minWidth: 0 }}>{entry.document_id}</span>
        <span className="muted">·</span>
        <span className="mono muted" style={{ overflowWrap: "anywhere", minWidth: 0 }}>{entry.chunk_id}</span>
        {entry.score != null && (
          <span className="muted" style={{ marginLeft: "auto" }}>score {Number(entry.score).toFixed(4)}</span>
        )}
      </div>
      <div className="text-sm mt-1" style={{
        whiteSpace: "pre-wrap",
        lineHeight: 1.45,
        overflowWrap: "anywhere",
        wordBreak: "break-word",
      }}>
        {entry.text}
      </div>
      {entry.meta && Object.keys(entry.meta).length > 0 && (
        <div className="muted text-sm mt-1 mono" style={{
          fontSize: 10.5,
          overflowWrap: "anywhere",
          wordBreak: "break-all",
        }}>
          {Object.entries(entry.meta).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" · ")}
        </div>
      )}
    </div>
  );
}


// Modal: list every indexed entry for a collection. Pulls from
// /collections/{id}/indexed_documents which walks the vector store
// (works for both user-owned and system collections).
function KN_CollectionListModal({ collection, pushToast, onClose }) {
  const { useResource, apiFetch } = window.primerApi;
  const PAGE_SIZE = 25;
  const [offset, setOffset] = React.useState(0);
  const indexed = useResource(
    `collection-indexed-list:${collection.id}:${offset}`,
    (signal) => apiFetch(
      "GET",
      `/collections/${encodeURIComponent(collection.id)}/indexed_documents?limit=${PAGE_SIZE}&offset=${offset}`,
      null,
      { signal },
    ),
    { pollMs: null, deps: [collection.id, offset] },
  );
  const items = (indexed.data?.items || []).map((r) => ({
    document_id: r.document_id,
    chunk_id: r.chunk_id,
    text: r.text,
    meta: r.meta,
    score: null,
  }));
  const total = indexed.data?.total ?? null;
  const showingFrom = total != null && total > 0 ? offset + 1 : 0;
  const showingTo = total != null
    ? Math.min(offset + PAGE_SIZE, total)
    : offset + items.length;
  const hasPrev = offset > 0;
  const hasNext = total != null
    ? (offset + PAGE_SIZE) < total
    : items.length >= PAGE_SIZE;

  return (
    <Modal
      title={
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Icon name="doc" size={13} className="muted" />
          <span>Documents in <span className="mono">{collection.id}</span></span>
          {collection.system && <span className="pill" style={{ marginLeft: 4 }}><span className="dot"></span>system</span>}
        </span>
      }
      onClose={onClose}
      footer={
        <div style={{ display: "flex", alignItems: "center", gap: 12, flex: 1 }}>
          <span className="muted text-sm tabular">
            {total == null
              ? (indexed.loading ? "Loading…" : "—")
              : total === 0
                ? "0 entries"
                : `${showingFrom}-${showingTo} of ${total}`}
          </span>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            <Btn size="sm" kind="ghost" icon="chevron-left"
              disabled={!hasPrev || indexed.loading}
              onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>Prev</Btn>
            <Btn size="sm" kind="ghost"
              disabled={!hasNext || indexed.loading}
              onClick={() => setOffset((o) => o + PAGE_SIZE)}>Next <Icon name="chevron-right" size={12} /></Btn>
            <Btn kind="ghost" onClick={onClose}>Close</Btn>
          </div>
        </div>
      }
    >
      <div style={{
        width: "min(80vw, 880px)",
        maxWidth: "100%",
        minWidth: 0,
        overflowX: "hidden",
      }}>
        <div className="muted text-sm mb-3" style={{ overflowWrap: "anywhere" }}>
          <span className="mono">GET /v1/collections/{collection.id}/indexed_documents</span>
        </div>
        {indexed.loading && items.length === 0 && (
          <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
        )}
        {indexed.error && (
          <Banner kind="error" title={indexed.error.title || "Failed to load entries"} detail={indexed.error.detail || indexed.error.message} />
        )}
        {!indexed.loading && items.length === 0 && !indexed.error && (
          <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
            {offset === 0 ? "No entries indexed yet." : "No more entries."}
          </div>
        )}
        {items.length > 0 && (
          <div style={{ maxHeight: 480, overflow: "auto", overflowX: "hidden" }}>
            {items.map((entry, i) => (
              <KN_EntryRow key={offset + i} entry={entry} index={offset + i} />
            ))}
          </div>
        )}
      </div>
    </Modal>
  );
}


// Modal: search the collection. Search box at the top; results
// underneath (still inside the modal, but separated from the inline
// detail view so the chrome stays clean).
function KN_CollectionSearchModal({ collection, pushToast, onClose }) {
  const { useMutation, apiFetch } = window.primerApi;
  const [query, setQuery] = React.useState("");
  const [topK, setTopK] = React.useState(10);
  const [hits, setHits] = React.useState(null);
  const [latencyMs, setLatencyMs] = React.useState(null);

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
        setLatencyMs(wallMs);
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

  const run = () => {
    if (!query.trim()) return;
    search.mutate({ query, top_k: topK });
  };

  return (
    <Modal
      title={
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Icon name="search" size={13} className="muted" />
          <span>Search <span className="mono">{collection.id}</span></span>
          {collection.system && <span className="pill" style={{ marginLeft: 4 }}><span className="dot"></span>system</span>}
        </span>
      }
      onClose={onClose}
      footer={<Btn kind="ghost" onClick={onClose}>Close</Btn>}
    >
      <div style={{
        width: "min(80vw, 880px)",
        maxWidth: "100%",
        minWidth: 0,
        overflowX: "hidden",
      }}>
        <div className="muted text-sm mb-3" style={{ overflowWrap: "anywhere" }}>
          <span className="mono">POST /v1/collections/{collection.id}/search</span>
        </div>
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
            autoFocus
          />
          <Btn kind="primary" icon="search" disabled={!query.trim() || search.loading} onClick={run}>
            {search.loading ? "Searching…" : "Search"}
          </Btn>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 14, marginTop: 10, fontSize: 12 }}>
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
          {latencyMs != null && (
            <span className="muted tabular">· {latencyMs} ms · {hits?.length ?? 0} hit{hits?.length === 1 ? "" : "s"}</span>
          )}
        </div>

        {hits != null && hits.length === 0 && (
          <div className="muted text-sm mt-3">No matches.</div>
        )}
        {hits != null && hits.length > 0 && (
          <div className="mt-3" style={{ maxHeight: 440, overflow: "auto" }}>
            {hits.map((h, i) => <KN_EntryRow key={i} entry={h} index={i} />)}
          </div>
        )}
      </div>
    </Modal>
  );
}

function KN_NewCollectionModal({ embedProviders, sspProviders = [], pushToast, onClose, onCreate, existing }) {
  const isEdit = !!existing;
  const { useMutation, apiFetch } = window.primerApi;
  const [id, setId] = React.useState(existing?.id || "");
  const [description, setDescription] = React.useState(existing?.description || "");
  const [providerId, setProviderId] = React.useState(existing?.embedder?.provider_id || "");
  const [model, setModel] = React.useState(existing?.embedder?.model || "");
  const [searchProviderId, setSearchProviderId] = React.useState(existing?.search_provider_id || "");
  const [fieldErrors, setFieldErrors] = React.useState({});

  React.useEffect(() => {
    if (!providerId && embedProviders.length > 0) setProviderId(embedProviders[0].id);
  }, [embedProviders, providerId]);
  React.useEffect(() => {
    if (!searchProviderId && sspProviders.length > 0) setSearchProviderId(sspProviders[0].id);
  }, [sspProviders, searchProviderId]);

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
    (body) => isEdit
      ? apiFetch("PUT", "/collections/" + encodeURIComponent(existing.id), body)
      : apiFetch("POST", "/collections", body),
    {
      invalidates: isEdit
        ? ["collections:list", "collection-detail:" + (existing?.id || "")]
        : ["collections:list"],
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
            title: err?.title || (isEdit ? "Save failed" : "Create failed"),
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
      ...(isEdit ? { id: existing.id } : (id ? { id } : {})),
      description: description || null,
      embedder: { provider_id: providerId, model },
      search_provider_id: searchProviderId,
    };
    try { await create.mutate(body); } catch (_e) { /* surfaced via onError */ }
  };

  return (
    <Modal
      title={isEdit ? `Edit collection · ${existing.id}` : "New collection"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "check" : "plus"} onClick={submit} disabled={!providerId || !model || !searchProviderId || create.loading}>
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
      <div className="field">
        <label className="field-label">Search provider</label>
        <select
          className="select"
          value={searchProviderId}
          onChange={(e) => setSearchProviderId(e.target.value)}
          disabled={isEdit}
          style={{ width: "100%" }}
        >
          <option value="">-- pick a search provider --</option>
          {sspProviders.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        {sspProviders.length === 0 && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No semantic-search providers configured. Create one at /ssp first.
          </div>
        )}
        <div className="field-help">
          {isEdit
            ? "Bound at create; immutable thereafter."
            : "Backs this collection's vector index. Immutable after create."}
        </div>
        {fieldErrors["body.search_provider_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.search_provider_id"]}</div>}
      </div>
    </Modal>
  );
}

// ============================================================================
// Documents page
// ============================================================================

function DocumentsPage({ pushToast, filterCollection, onClearFilter }) {
  const { useResource, useRouter, useViewport, apiFetch } = window.primerApi;
  const { query, navigate } = useRouter();
  const { isMobile } = useViewport();
  // Prefer the explicit prop (app.jsx passes docsFilterCollection) and fall
  // back to the router query for deep-link cases.
  const collectionFilter = (filterCollection != null && filterCollection !== "")
    ? filterCollection
    : (query.collection || "");

  const collections = useResource(
    "collections:list",
    (signal) => apiFetch("GET", "/collections?limit=200", null, { signal }),
    { pollMs: null },
  );

  // System (internal) collections don't have Document storage rows;
  // their content lives in the vector store. Route the per-collection
  // listing through /indexed_documents instead for those — the
  // global "all collections" view stays on /documents (storage rows
  // across the entire deployment).
  const selectedCollection = (collections.data?.items ?? []).find(
    (c) => c.id === collectionFilter,
  );
  const isSystemFilter = !!selectedCollection?.system;

  // Pagination — keep prev/next driven by offset. Reset when the
  // collection filter changes so we don't end up past-the-end after a
  // switch from a large collection to a small one.
  const PAGE_SIZE = 50;
  const [offset, setOffset] = React.useState(0);
  React.useEffect(() => { setOffset(0); }, [collectionFilter, isSystemFilter]);

  const list = useResource(
    `documents:list:${collectionFilter}:${isSystemFilter ? "vec" : "store"}:${offset}`,
    (signal) => apiFetch(
      "GET",
      collectionFilter
        ? (isSystemFilter
            ? `/collections/${encodeURIComponent(collectionFilter)}/indexed_documents?limit=${PAGE_SIZE}&offset=${offset}`
            : `/collections/${encodeURIComponent(collectionFilter)}/documents?limit=${PAGE_SIZE}&offset=${offset}`)
        : `/documents?limit=${PAGE_SIZE}&offset=${offset}`,
      null,
      { signal },
    ),
    { pollMs: null, deps: [collectionFilter, isSystemFilter, offset] },
  );
  const total = list.data?.total ?? null;
  const showingFrom = total != null && total > 0 ? offset + 1 : 0;
  const showingTo = total != null
    ? Math.min(offset + PAGE_SIZE, total)
    : offset + (list.data?.items?.length ?? 0);
  const hasPrev = offset > 0;
  const hasNext = total != null
    ? (offset + PAGE_SIZE) < total
    : (list.data?.items?.length ?? 0) >= PAGE_SIZE;

  const [textFilter, setTextFilter] = React.useState("");
  const [createOpen, setCreateOpen] = React.useState(false);
  const [editing, setEditing] = React.useState(null);

  // Normalise to a single row shape. /documents returns Document
  // storage rows {id, collection_id, name, meta}; /indexed_documents
  // returns vector entries {document_id, chunk_id, text, meta}. Map
  // the vector shape onto the table's columns so the rest of the
  // page doesn't need to branch.
  const rawItems = list.data?.items ?? [];
  const items = isSystemFilter
    ? rawItems.map((r) => ({
        id: r.document_id,
        collection_id: collectionFilter,
        name: (r.text || "").slice(0, 80),
        meta: r.meta || {},
        _indexed: true,
        _chunk_id: r.chunk_id,
      }))
    : rawItems;
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
      {isMobile && collectionFilter && (
        <div
          className="knowledge-mobile-back"
          onClick={() => setCollectionFilter("")}
        >
          <Icon name="chevron-left" size={14} />
          <span>All collections</span>
          <span className="mono muted" style={{ marginLeft: 6 }}>· {collectionFilter}</span>
        </div>
      )}
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

      {isMobile ? (
        <CardList
          items={filtered}
          empty={items.length === 0 ? "No documents yet." : "No documents match."}
          renderCard={(d) => (
            <Card
              title={d.id}
              subtitle={d.name || ""}
              meta={d.collection_id}
              onClick={() => { if (!d._indexed) setEditing(d); }}
            />
          )}
        />
      ) : (
      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Collection</th>
              <th>Name</th>
              <th>Meta keys</th>
              <th style={{ width: 60, textAlign: "right" }}></th>
            </tr>
          </thead>
          <tbody>
            {list.loading && items.length === 0 ? (
              <tr><td colSpan={5} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</td></tr>
            ) : list.error && items.length === 0 ? (
              <tr><td colSpan={5} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{list.error.title || list.error.message}</span>
                {" · "}<a onClick={list.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              items.length === 0 ? (
                <tr><td colSpan={5}>
                  <KN_EmptyState
                    ico="doc"
                    head="No documents yet"
                    sub="Documents are ingested into a collection and indexed for similarity search."
                    cta={<Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Ingest document</Btn>}
                  />
                </td></tr>
              ) : (
                <tr><td colSpan={5} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
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
                  <td style={{ textAlign: "right", paddingRight: 12 }}>
                    {!d._indexed && (
                      <Btn size="sm" kind="ghost" icon="edit" onClick={() => setEditing(d)} title="Edit document" />
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      )}

      {isMobile && (
        <Fab icon="plus" label="New document" onClick={() => setCreateOpen(true)} />
      )}

      <div style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "10px 4px",
        fontSize: 12,
      }}>
        <span className="muted tabular">
          {total == null
            ? (list.loading ? "Loading…" : "—")
            : total === 0
              ? "0 documents"
              : `${showingFrom}-${showingTo} of ${total}`}
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn
            size="sm"
            kind="ghost"
            icon="chevron-left"
            disabled={!hasPrev || list.loading}
            onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
          >Prev</Btn>
          <Btn
            size="sm"
            kind="ghost"
            disabled={!hasNext || list.loading}
            onClick={() => setOffset((o) => o + PAGE_SIZE)}
          >Next <Icon name="chevron-right" size={12} /></Btn>
        </div>
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
      {editing && (
        <KN_NewDocumentModal
          collections={collections.data?.items ?? []}
          existing={editing}
          pushToast={pushToast}
          onClose={() => setEditing(null)}
          onCreate={(d) => {
            setEditing(null);
            if (typeof pushToast === "function") {
              pushToast({ kind: "info", title: "Document updated", detail: d.id });
            }
            list.refetch();
          }}
        />
      )}
    </div>
  );
}

function KN_NewDocumentModal({ collections, defaultCollection, pushToast, onClose, onCreate, existing }) {
  const isEdit = !!existing;
  const { useMutation, apiFetch } = window.primerApi;
  const _initialMeta = () => {
    if (!isEdit) return "";
    const { text: _t, ...rest } = existing.meta || {};
    return Object.keys(rest).length > 0 ? JSON.stringify(rest, null, 2) : "";
  };
  const _initialText = () => isEdit ? (existing.meta?.text || "") : "";

  const [collectionId, setCollectionId] = React.useState(
    existing?.collection_id || defaultCollection || ""
  );
  const [name, setName] = React.useState(existing?.name || "");
  const [text, setText] = React.useState(_initialText);
  const [metaJson, setMetaJson] = React.useState(_initialMeta);
  const [fieldErrors, setFieldErrors] = React.useState({});

  // Content-input mode: "paste" -> direct textarea editing,
  // "upload" -> drag-and-drop / file-picker that converts via
  // docling (or short-circuits for already-text formats like .md / .txt)
  // and then hands the result to the textarea. Default to upload on
  // create (operators usually have a file ready) and paste on edit
  // (no file to re-upload).
  const [contentMode, setContentMode] = React.useState(
    isEdit ? "paste" : "upload",
  );

  // File-upload conversion state.
  const [convertingFile, setConvertingFile] = React.useState(false);
  const [convertedFileName, setConvertedFileName] = React.useState(null);
  const [convertError, setConvertError] = React.useState(null);
  const [isDragOver, setIsDragOver] = React.useState(false);

  const handleConvertFile = async (f) => {
    setConvertingFile(true);
    setConvertError(null);
    try {
      const fd = new FormData();
      fd.append("file", f);
      const resp = await apiFetch(
        "POST",
        "/documents/_convert_file",
        fd,
      );
      setText(resp.text || "");
      setConvertedFileName(resp.filename || f.name);
      if (!name) setName(resp.filename || f.name);
      // Switch to paste mode so the operator can review + edit the
      // converted text immediately. The "from: <filename>" caption
      // shows alongside the textarea so the upload provenance is
      // still visible.
      setContentMode("paste");
    } catch (err) {
      setConvertError(
        (err && (err.detail || err.message)) || "Conversion failed",
      );
    } finally {
      setConvertingFile(false);
    }
  };

  const handleDragOver = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!isDragOver) setIsDragOver(true);
  };

  const handleDragLeave = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
    const f = e.dataTransfer?.files?.[0];
    if (f) handleConvertFile(f);
  };

  React.useEffect(() => {
    if (!collectionId && collections.length > 0) setCollectionId(collections[0].id);
  }, [collections, collectionId]);

  const create = useMutation(
    (body) => isEdit
      ? apiFetch("PUT", "/documents/" + encodeURIComponent(existing.id), body)
      : apiFetch("POST", "/documents", body),
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
            title: err?.title || (isEdit ? "Save failed" : "Create failed"),
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
    // Document.id is required; preserve on edit, mint short-form on create.
    const docId = isEdit ? existing.id : ("doc-" + Math.random().toString(16).slice(2, 10));
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
      title={isEdit ? `Edit document · ${existing.id}` : "Ingest document"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "check" : "plus"} onClick={submit} disabled={!collectionId || create.loading}>
            {create.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save changes" : "Create")}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">Collection {isEdit && <span className="hint">locked — cannot move documents between collections</span>}</label>
        <select
          className="select"
          value={collectionId}
          onChange={(e) => setCollectionId(e.target.value)}
          disabled={isEdit}
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
        <label className="field-label">
          Text <span className="hint">stored under meta.text for v1</span>
        </label>
        {/* Sub-tabs: paste-text vs drag-and-drop upload */}
        <div style={{
          display: "flex", gap: 0, marginBottom: 8,
          borderBottom: "1px solid var(--border)",
        }}>
          <button
            type="button"
            onClick={() => setContentMode("paste")}
            style={{
              padding: "6px 14px", fontSize: 12,
              background: contentMode === "paste" ? "var(--bg-1, var(--bg))" : "transparent",
              border: "none",
              borderBottom: contentMode === "paste"
                ? "2px solid var(--accent, #38bdf8)"
                : "2px solid transparent",
              color: contentMode === "paste" ? "var(--text-1, var(--text))" : "var(--text-3)",
              cursor: "pointer", fontWeight: contentMode === "paste" ? 600 : 400,
            }}
          >Paste text</button>
          <button
            type="button"
            onClick={() => setContentMode("upload")}
            style={{
              padding: "6px 14px", fontSize: 12,
              background: contentMode === "upload" ? "var(--bg-1, var(--bg))" : "transparent",
              border: "none",
              borderBottom: contentMode === "upload"
                ? "2px solid var(--accent, #38bdf8)"
                : "2px solid transparent",
              color: contentMode === "upload" ? "var(--text-1, var(--text))" : "var(--text-3)",
              cursor: "pointer", fontWeight: contentMode === "upload" ? 600 : 400,
            }}
          >Upload file</button>
          {convertedFileName && !convertingFile && contentMode === "paste" && (
            <span className="muted text-sm" style={{
              marginLeft: "auto", alignSelf: "center", padding: "0 6px",
            }} title={convertedFileName}>
              from: {convertedFileName}
            </span>
          )}
        </div>

        {contentMode === "paste" ? (
          <textarea
            className="textarea"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={8}
            placeholder="Paste or type your document text here. Markdown is preserved as-is when saved under meta.text."
          />
        ) : (
          <div
            onDragOver={handleDragOver}
            onDragEnter={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            style={{
              border: isDragOver
                ? "2px dashed var(--accent, #38bdf8)"
                : "2px dashed var(--border)",
              borderRadius: 6,
              padding: "32px 16px",
              textAlign: "center",
              background: isDragOver
                ? "var(--accent-dim, rgba(56, 189, 248, 0.08))"
                : "var(--bg-1, var(--bg))",
              minHeight: 160,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              gap: 8,
              transition: "background 80ms, border-color 80ms",
            }}
          >
            {convertingFile ? (
              <div className="muted text-sm">Converting…</div>
            ) : (
              <>
                <div style={{ fontSize: 13, marginBottom: 4 }}>
                  {isDragOver ? "Release to upload" : "Drag and drop a file here"}
                </div>
                <div className="muted text-sm">or</div>
                <label
                  className="btn"
                  style={{ cursor: "pointer", fontSize: 12 }}
                  title="PDF, DOCX, PPTX, XLSX, HTML, .md, .txt, images, ... - text formats are stored as-is; docling converts binary formats to markdown"
                >
                  <input
                    type="file"
                    accept=".pdf,.docx,.pptx,.xlsx,.html,.htm,.md,.markdown,.txt,.png,.jpg,.jpeg"
                    style={{ display: "none" }}
                    onChange={(e) => {
                      const f = e.target.files && e.target.files[0];
                      if (!f) return;
                      handleConvertFile(f);
                      e.target.value = "";
                    }}
                  />
                  Choose a file
                </label>
                <div className="muted text-sm" style={{ marginTop: 6, fontSize: 11 }}>
                  PDF · DOCX · PPTX · XLSX · HTML · .md · .txt · images · ≤ 32 MB
                </div>
                {convertedFileName && (
                  <div className="muted text-sm" style={{ marginTop: 4 }} title={convertedFileName}>
                    last uploaded: {convertedFileName}
                  </div>
                )}
                {convertError && (
                  <div style={{ color: "var(--red)", fontSize: 11, marginTop: 6 }}>
                    {convertError}
                  </div>
                )}
              </>
            )}
          </div>
        )}
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
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  // Source the toast push from the primerApi namespace (toast.js publishes
  // window.primerApi.toastPush) so this works whether or not app.jsx forwards
  // a pushToast prop into SearchBench.
  const pushToast = window.primerApi?.toastPush || (() => {});

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
