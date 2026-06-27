/* global React, Icon, Btn, StatusPill, Modal, Banner, CardList, Card, Fab, relativeTime */

// Knowledge: Collections + Documents wired to the real API.
// The Designer's mock-data scaffold was replaced in Phase 2 — every fetch
// goes through window.primerApi.{apiFetch, useResource, useMutation,
// useRouter}. Cache-key convention follows other components:
//
//   collections:list                — GET /collections?limit=200
//   collections:embedding-providers — GET /embedding_providers?limit=200
//   documents:list:${collectionId}  — GET /documents (filter by collection)
//   collection-docs-count:${id}     — per-collection doc count probe
//
// Babel-standalone shares the global scope across <script> tags so every
// top-level binding in this file is prefixed with KN_ to avoid name clashes
// with other components (TS_*, AG_*, WS_*, etc.).

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

// ============================================================================
// Collections page
// ============================================================================

function CollectionsPage({ pushToast, onOpen, onNavigate }) {
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
  const cerProviders = useResource(
    "collections:cer-providers",
    (signal) => apiFetch("GET", "/cross_encoder_providers?limit=200", null, { signal }),
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
            cerProviders={cerProviders.data?.items ?? []}
            onOpenDocs={() => {
              if (typeof onOpen === "function") onOpen(sel.id);
              else navigate("/knowledge/documents", { collection: sel.id });
            }}
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
          cerProviders={cerProviders.data?.items ?? []}
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

function KN_CollectionDetail({ c, pushToast, onOpenDocs, onNavigate, embedProviders, sspProviders, cerProviders }) {
  const { useResource, apiFetch } = window.primerApi;
  const isSystem = !!c.system;
  const isManaged = !!c.harness_id;
  const [listOpen, setListOpen] = React.useState(false);
  const [browseOpen, setBrowseOpen] = React.useState(false);
  const [searchOpen, setSearchOpen] = React.useState(false);
  const [editOpen, setEditOpen] = React.useState(false);

  // For the docs count: system collections live in the vector store
  // only — Document storage rows are always empty, so probing them
  // would always render 0. Probe the vector-store enumeration instead.
  // User collections store path-addressed documents; the list route
  // returns the full path list with no pagination, so the count is just
  // the length of the returned {documents:[...]} array.
  const storageDocs = useResource(
    `collection-docs-count:${c.id}`,
    (signal) => isSystem
      ? Promise.resolve(null)
      : apiFetch(
          "GET",
          `/collections/${encodeURIComponent(c.id)}/documents`,
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
    : (storageDocs.data?.documents ? storageDocs.data.documents.length : "-");

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
            {/* User collections are path-addressed: "Documents" opens the
                file-tree explorer. System/internal collections have no
                editable path tree, so they keep the indexed-document list. */}
            <Btn
              size="sm"
              kind="primary"
              icon="doc"
              onClick={() => (isSystem ? setListOpen(true) : setBrowseOpen(true))}
            >
              {isSystem ? "List documents" : "Documents"}
            </Btn>
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
      {browseOpen && (
        <KN_CollectionDocBrowserModal
          collection={c}
          pushToast={pushToast}
          onClose={() => setBrowseOpen(false)}
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
          cerProviders={cerProviders || []}
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
        {entry.chunk_id != null && entry.chunk_id !== "" && (
          <>
            <span className="muted">·</span>
            <span className="mono muted" style={{ overflowWrap: "anywhere", minWidth: 0 }}>{entry.chunk_id}</span>
          </>
        )}
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


// Modal: list a collection's documents. System (internal) collections
// have no Document storage rows - their content lives in the vector
// store - so those pull from /indexed_documents (paginated chunk
// enumeration). User collections store path-addressed documents, so
// those defer to KN_UserCollectionListView which consumes the
// path-list shape ({documents:[{path, document_id, size}]}, no pager).
function KN_CollectionListModal({ collection, pushToast, onClose }) {
  const isSystem = !!collection.system;
  const titleBar = (
    <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <Icon name="doc" size={13} className="muted" />
      <span>Documents in <span className="mono">{collection.id}</span></span>
      {collection.system && <span className="pill" style={{ marginLeft: 4 }}><span className="dot"></span>system</span>}
    </span>
  );
  if (isSystem) {
    return <KN_SystemCollectionListView collection={collection} pushToast={pushToast} onClose={onClose} titleBar={titleBar} />;
  }
  return <KN_UserCollectionListView collection={collection} pushToast={pushToast} onClose={onClose} titleBar={titleBar} />;
}

// System collections: paginated chunk enumeration from /indexed_documents,
// which still returns the {items, total, offset, limit} OffsetPage shape.
function KN_SystemCollectionListView({ collection, pushToast, onClose, titleBar }) {
  const { useResource, apiFetch } = window.primerApi;
  const PAGE_SIZE = 25;
  const [offset, setOffset] = React.useState(0);
  const indexed = useResource(
    `collection-list:${collection.id}:indexed_documents:${offset}`,
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
      title={titleBar}
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

// User collections: the documents route is path-addressed and returns the
// full list with NO pagination as {documents:[{path, document_id, size}]}
// (optional ?prefix= filter). Render one row per path (path + size),
// clicking a row opens the body via GET ...?path=<p> -> {document, content}.
function KN_UserCollectionListView({ collection, pushToast, onClose, titleBar }) {
  const { useResource, apiFetch } = window.primerApi;
  const cid = collection.id;
  const enc = encodeURIComponent;
  const [prefix, setPrefix] = React.useState("");
  const [appliedPrefix, setAppliedPrefix] = React.useState("");
  const [selectedPath, setSelectedPath] = React.useState(null);

  const list = useResource(
    `collection-doc-list:${cid}:${appliedPrefix}`,
    (signal) => apiFetch(
      "GET",
      `/collections/${enc(cid)}/documents` +
        (appliedPrefix ? `?prefix=${enc(appliedPrefix)}` : ""),
      null,
      { signal },
    ),
    { pollMs: null, deps: [cid, appliedPrefix] },
  );
  const rows = _knIndentRows(list.data?.documents ?? []);
  const count = list.data?.documents ? list.data.documents.length : null;

  // Selected document body, fetched on demand by path. Mirrors the path
  // browser: GET ...?path=<p> -> {document:{...}, content}.
  const doc = useResource(
    `collection-doc-body:${cid}:${selectedPath || ""}`,
    (signal) => selectedPath
      ? apiFetch("GET", `/collections/${enc(cid)}/documents?path=${enc(selectedPath)}`, null, { signal })
      : Promise.resolve(null),
    { pollMs: null, deps: [cid, selectedPath] },
  );
  const docMeta = doc.data?.document || null;

  return (
    <Modal
      title={titleBar}
      onClose={onClose}
      footer={
        <div style={{ display: "flex", alignItems: "center", gap: 12, flex: 1 }}>
          <span className="muted text-sm tabular">
            {count == null
              ? (list.loading ? "Loading…" : "-")
              : `${count} document${count === 1 ? "" : "s"}`}
          </span>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
            <Btn kind="ghost" onClick={onClose}>Close</Btn>
          </div>
        </div>
      }
      width="min(86vw, 1000px)"
    >
      <div style={{ width: "100%", minWidth: 0 }}>
        <div className="muted text-sm mb-3" style={{ overflowWrap: "anywhere" }}>
          <span className="mono">GET /v1/collections/{cid}/documents?prefix=</span>
        </div>

        {/* Prefix filter */}
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <div className="input-icon" style={{ flex: 1 }}>
            <Icon name="filter" size={13} className="icon" />
            <input
              className="input"
              placeholder="Filter by path prefix… (Enter to apply)"
              value={prefix}
              onChange={(e) => setPrefix(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") setAppliedPrefix(prefix.trim()); }}
              onBlur={() => setAppliedPrefix(prefix.trim())}
            />
          </div>
          {appliedPrefix && (
            <Btn size="sm" kind="ghost" icon="x" onClick={() => { setPrefix(""); setAppliedPrefix(""); }}>Clear</Btn>
          )}
        </div>

        {/* Two-pane: path list + read-only content */}
        <div className="kn-doc-browser" style={{ display: "grid", gridTemplateColumns: "minmax(220px, 320px) 1fr", gap: 14, alignItems: "start" }}>
          <div className="tbl-wrap" style={{ maxHeight: 460, overflow: "auto" }}>
            {list.loading && rows.length === 0 ? (
              <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>Loading…</div>
            ) : list.error ? (
              <div style={{ padding: 16, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{list.error.title || list.error.message}</span>
                {" · "}<a onClick={list.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </div>
            ) : rows.length === 0 ? (
              <div className="muted text-sm" style={{ padding: 16, textAlign: "center" }}>
                {appliedPrefix
                  ? `No documents under "${appliedPrefix}".`
                  : "No documents in this collection yet."}
              </div>
            ) : (
              rows.map((r) => (
                <div
                  key={r.path}
                  onClick={() => setSelectedPath(r.path)}
                  className={selectedPath === r.path ? "selected" : ""}
                  style={{
                    cursor: "pointer",
                    padding: "6px 8px",
                    paddingLeft: 8 + r.depth * 14,
                    borderBottom: "1px solid var(--border)",
                    background: selectedPath === r.path ? "var(--accent-dim, rgba(56,189,248,0.08))" : "transparent",
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    minWidth: 0,
                  }}
                  title={r.path}
                >
                  <Icon name="file" size={12} className="muted" />
                  <span className="mono text-sm" style={{ flex: 1, minWidth: 0, overflowWrap: "anywhere" }}>{r.leaf}</span>
                  <span className="muted tabular" style={{ fontSize: 10.5 }}>{r.size}</span>
                </div>
              ))
            )}
          </div>

          <div style={{ minWidth: 0 }}>
            {!selectedPath ? (
              <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>
                Select a document on the left to view its content.
              </div>
            ) : doc.loading && !doc.data ? (
              <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>Loading…</div>
            ) : doc.error ? (
              <Banner kind="error" title={doc.error.title || "Failed to load document"} detail={doc.error.detail || doc.error.message} />
            ) : (
              <div className="col" style={{ gap: 10, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
                  <span className="mono" style={{ fontWeight: 600, overflowWrap: "anywhere", minWidth: 0 }}>{selectedPath}</span>
                </div>
                <div className="kv text-sm" style={{ gridTemplateColumns: "70px 1fr" }}>
                  <dt>title</dt><dd>{docMeta?.title || <span className="muted">{_leafOf(selectedPath)}</span>}</dd>
                  <dt>id</dt><dd className="mono muted text-sm">{docMeta?.id || "-"}</dd>
                </div>
                <pre style={{
                  whiteSpace: "pre-wrap",
                  overflowWrap: "anywhere",
                  wordBreak: "break-word",
                  background: "var(--bg-1, var(--bg))",
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                  padding: 12,
                  margin: 0,
                  maxHeight: 360,
                  overflow: "auto",
                  fontSize: 12.5,
                  lineHeight: 1.5,
                }}>{doc.data?.content || ""}</pre>
              </div>
            )}
          </div>
        </div>
      </div>
    </Modal>
  );
}


// ============================================================================
// Path-addressed document browser + editor (Task 15)
// ============================================================================
//
// Documents in a (user) collection are addressed by a POSIX-like path; the
// body lives in the content store and is reached through the path-addressed
// REST surface (Task 11):
//
//   GET    /collections/{cid}/documents?prefix=<p>  -> {documents:[{path,document_id,size}]}
//   GET    /collections/{cid}/documents?path=<p>    -> {document:{...path,title...}, content}
//   PUT    /collections/{cid}/documents?path=<p>     {content, title?, meta?}
//   DELETE /collections/{cid}/documents?path=<p>
//   POST   /collections/{cid}/documents/move         {from, to}
//
// This modal is the operator-facing browser: an indented path list on the
// left, a read/edit content pane on the right, plus create / delete / move
// affordances. Errors come back as RFC7807 (problem+json) and surface via
// the shared toast (err.title / err.detail).

// Build a flat, indented view of the path list. Kept for any callers that
// still reference it, but the browser modal now uses _knBuildTree instead.
function _knIndentRows(entries) {
  const sorted = [...entries].sort((a, b) =>
    (a.path || "").localeCompare(b.path || ""),
  );
  return sorted.map((e) => {
    const segs = (e.path || "").split("/");
    return {
      path: e.path,
      document_id: e.document_id,
      size: e.size,
      depth: Math.max(0, segs.length - 1),
      leaf: segs[segs.length - 1],
    };
  });
}

// Convert a flat [{path, document_id, size}] list into a nested tree:
//   { name, children: Map<name, node>, file: null | {path, size} }
// Folder nodes have file=null; leaf nodes have file set and children empty.
function _knBuildTree(entries) {
  const root = { name: "", children: new Map(), file: null };
  for (const e of entries) {
    const segs = (e.path || "").split("/").filter(Boolean);
    let node = root;
    for (let i = 0; i < segs.length; i++) {
      const seg = segs[i];
      if (!node.children.has(seg)) {
        node.children.set(seg, { name: seg, children: new Map(), file: null });
      }
      node = node.children.get(seg);
      if (i === segs.length - 1) {
        node.file = { path: e.path, size: e.size };
      }
    }
  }
  return root;
}

// Recursive tree-node component for the knowledge doc browser.
// Mirrors WS_DirNode / WS_FileRow from workspaces.jsx.
function KN_DocTreeNode({ node, depth, openDirs, toggleDir, selectedPath, selectPath }) {
  const isRoot = depth === 0;
  const isFolder = node.file === null;
  const isOpen = openDirs.has(node.name === "" ? "__root__" : _knNodeKey(node, depth));

  // Sort children: folders first, then files, alphabetical within each group.
  const sorted = React.useMemo(() => {
    const arr = [...node.children.values()];
    arr.sort((a, b) => {
      const af = a.file === null;
      const bf = b.file === null;
      if (af && !bf) return -1;
      if (!af && bf) return 1;
      return a.name.localeCompare(b.name);
    });
    return arr;
  }, [node.children]);

  const [hover, setHover] = React.useState(false);

  if (isRoot) {
    // Root renders children directly with no row of its own.
    return (
      <div>
        {sorted.map((child) => (
          <KN_DocTreeNode
            key={child.name}
            node={child}
            depth={depth + 1}
            openDirs={openDirs}
            toggleDir={toggleDir}
            selectedPath={selectedPath}
            selectPath={selectPath}
          />
        ))}
      </div>
    );
  }

  const isSelected = !isFolder && node.file && node.file.path === selectedPath;
  const indentPx = 12 + Math.max(0, depth - 1) * 14;
  const size = !isFolder && node.file ? node.file.size : null;

  const handleClick = () => {
    if (isFolder) {
      toggleDir(_knNodeKey(node, depth));
    } else if (node.file) {
      selectPath(node.file.path);
    }
  };

  return (
    <div>
      <div
        onClick={handleClick}
        onMouseEnter={(ev) => { setHover(true); if (!isSelected) ev.currentTarget.style.background = "var(--bg-hover)"; }}
        onMouseLeave={(ev) => { setHover(false); if (!isSelected) ev.currentTarget.style.background = "transparent"; }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          padding: "3px 12px",
          paddingLeft: indentPx,
          cursor: "pointer",
          background: isSelected ? "var(--accent-dim)" : "transparent",
          color: isSelected ? "var(--text)" : "var(--text-2)",
          fontSize: 12.5,
        }}
      >
        {isFolder ? (
          <>
            <Icon name={isOpen ? "chevron-down" : "chevron-right"} size={10} className="muted" />
            <Icon name="box" size={12} style={{ color: "var(--text-3)" }} />
          </>
        ) : (
          <>
            <span style={{ width: 10 }} />
            <Icon name="doc" size={11} style={{ color: "var(--text-4)" }} />
          </>
        )}
        <span className="mono" style={{ fontSize: 12, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{node.name}</span>
        {!isFolder && size != null && (
          <span className="muted mono" style={{ fontSize: 10.5, marginLeft: "auto", flexShrink: 0 }}>
            {size === 0 ? "0" : size < 1024 ? `${size}B` : `${(size / 1024).toFixed(1)}K`}
          </span>
        )}
      </div>
      {isFolder && isOpen && sorted.map((child) => (
        <KN_DocTreeNode
          key={child.name}
          node={child}
          depth={depth + 1}
          openDirs={openDirs}
          toggleDir={toggleDir}
          selectedPath={selectedPath}
          selectPath={selectPath}
        />
      ))}
    </div>
  );
}

// Stable key for a tree node based on name + depth (avoids full-path threading).
// Since folder names at the same depth could theoretically collide in separate
// branches we prefix with depth, which is sufficient for toggle identity.
function _knNodeKey(node, depth) {
  return `${depth}:${node.name}`;
}

function KN_CollectionDocBrowserModal({ collection, pushToast, onClose }) {
  const { useResource, useMutation, apiFetch } = window.primerApi;
  const cid = collection.id;
  const enc = encodeURIComponent;

  const [prefix, setPrefix] = React.useState("");
  // Debounce-free: the prefix filter only refetches on Enter / blur so a
  // half-typed prefix doesn't spam the endpoint. `appliedPrefix` is what the
  // resource key is built from.
  const [appliedPrefix, setAppliedPrefix] = React.useState("");
  const [selectedPath, setSelectedPath] = React.useState(null);
  const [editing, setEditing] = React.useState(false);
  const [draftTitle, setDraftTitle] = React.useState("");
  const [draftContent, setDraftContent] = React.useState("");
  const [creating, setCreating] = React.useState(false);
  const [newPath, setNewPath] = React.useState("");
  const [deleting, setDeleting] = React.useState(false);
  const [moving, setMoving] = React.useState(false);
  const [moveTo, setMoveTo] = React.useState("");
  const [openDirs, setOpenDirs] = React.useState(() => new Set());
  const [viewMode, setViewMode] = React.useState("rendered");

  const toggleDir = React.useCallback((key) => {
    setOpenDirs((prev) => {
      const next = new Set(prev);
      if (next.has(key)) { next.delete(key); } else { next.add(key); }
      return next;
    });
  }, []);

  const listKey = `coll-doc-paths:${cid}:${appliedPrefix}`;
  const list = useResource(
    listKey,
    (signal) => apiFetch(
      "GET",
      `/collections/${enc(cid)}/documents` +
        (appliedPrefix ? `?prefix=${enc(appliedPrefix)}` : ""),
      null,
      { signal },
    ),
    { pollMs: null, deps: [cid, appliedPrefix] },
  );

  const rows = _knIndentRows(list.data?.documents ?? []);

  // Content pane: fetch the selected document body on demand. The resource
  // re-runs whenever `selectedPath` changes (deps), so opening a different
  // path swaps the body.
  const doc = useResource(
    `coll-doc-body:${cid}:${selectedPath || ""}`,
    (signal) => selectedPath
      ? apiFetch("GET", `/collections/${enc(cid)}/documents?path=${enc(selectedPath)}`, null, { signal })
      : Promise.resolve(null),
    { pollMs: null, deps: [cid, selectedPath] },
  );

  // When a freshly-loaded document lands while we are NOT editing, seed the
  // draft buffers so an Edit click starts from the stored values.
  React.useEffect(() => {
    if (doc.data && !editing) {
      setDraftTitle(doc.data.document?.title || "");
      setDraftContent(doc.data.content || "");
    }
  }, [doc.data]);  // eslint-disable-line react-hooks/exhaustive-deps

  const openPath = (p) => {
    setSelectedPath(p);
    setEditing(false);
    setCreating(false);
    setDeleting(false);
    setMoving(false);
  };

  const errToast = _knToastErr(pushToast);

  // useMutation.onSuccess receives only the response (no input args), so the
  // post-success state transitions live in the submit handlers below where
  // the inputs are in scope. Errors fall through to onError -> shared toast.
  // PUT upserts: same endpoint creates a new path or replaces an existing one.
  const save = useMutation(
    ({ path, content, title }) => apiFetch(
      "PUT",
      `/collections/${enc(cid)}/documents?path=${enc(path)}`,
      { content, title: title || null },
    ),
    { invalidates: [listKey], onError: errToast },
  );

  const del = useMutation(
    (path) => apiFetch("DELETE", `/collections/${enc(cid)}/documents?path=${enc(path)}`),
    { invalidates: [listKey], onError: (err) => { setDeleting(false); errToast(err); } },
  );

  const move = useMutation(
    ({ from, to }) => apiFetch("POST", `/collections/${enc(cid)}/documents/move`, { from, to }),
    { invalidates: [listKey], onError: (err) => { setMoving(false); errToast(err); } },
  );

  const startCreate = () => {
    setCreating(true);
    setEditing(false);
    setSelectedPath(null);
    setNewPath("");
    setDraftTitle("");
    setDraftContent("");
  };

  const submitCreate = async () => {
    const p = newPath.trim();
    if (!p) return;
    try {
      const resp = await save.mutate({ path: p, content: draftContent, title: draftTitle });
      const savedPath = (resp && resp.document && resp.document.path) || p;
      if (typeof pushToast === "function") {
        pushToast({ kind: "success", title: "Document created", detail: savedPath });
      }
      setCreating(false);
      setSelectedPath(savedPath);
      list.refetch();
    } catch (_e) { /* surfaced via onError */ }
  };

  const submitEdit = async () => {
    if (!selectedPath) return;
    const p = selectedPath;
    try {
      await save.mutate({ path: p, content: draftContent, title: draftTitle });
      if (typeof pushToast === "function") {
        pushToast({ kind: "success", title: "Document saved", detail: p });
      }
      setEditing(false);
      doc.refetch();
      list.refetch();
    } catch (_e) { /* surfaced via onError */ }
  };

  const submitDelete = async () => {
    if (!selectedPath) return;
    const removed = selectedPath;
    try {
      await del.mutate(removed);
      if (typeof pushToast === "function") {
        pushToast({ kind: "success", title: "Document deleted", detail: removed });
      }
      setDeleting(false);
      setSelectedPath(null);
      list.refetch();
    } catch (_e) { /* surfaced via onError */ }
  };

  const submitMove = async () => {
    const to = moveTo.trim();
    if (!to || !selectedPath || to === selectedPath) return;
    const from = selectedPath;
    try {
      await move.mutate({ from, to });
      if (typeof pushToast === "function") {
        pushToast({ kind: "success", title: "Document moved", detail: `${from} -> ${to}` });
      }
      setMoving(false);
      setSelectedPath(to);
      list.refetch();
    } catch (_e) { /* surfaced via onError */ }
  };

  const docMeta = doc.data?.document || null;

  // Build tree from the flat document list for the file-explorer left pane.
  const treeRoot = React.useMemo(
    () => _knBuildTree(list.data?.documents ?? []),
    [list.data],
  );

  const isMarkdown = selectedPath && selectedPath.toLowerCase().endsWith(".md");

  return (
    <Modal
      title={
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Icon name="box" size={13} className="muted" />
          <span>Browse <span className="mono">{cid}</span></span>
        </span>
      }
      onClose={onClose}
      footer={
        <div style={{ display: "flex", alignItems: "center", gap: 12, flex: 1 }}>
          <span className="muted text-sm tabular">
            {list.data?.documents
              ? `${list.data.documents.length} document${list.data.documents.length === 1 ? "" : "s"}`
              : (list.loading ? "Loading…" : "-")}
          </span>
          <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            <Btn size="sm" kind="primary" icon="plus" onClick={startCreate}>New document</Btn>
            <Btn kind="ghost" onClick={onClose}>Close</Btn>
          </div>
        </div>
      }
      width="min(92vw, 1280px)"
    >
      {/* The .modal element is widened via Modal's width prop; fill it. */}
      <div style={{ width: "100%", minWidth: 0 }}>

        {/* Two-pane file explorer: tree left, content right */}
        <div style={{
          display: "grid",
          gridTemplateColumns: "300px 1fr",
          height: "calc(100vh - 220px)",
          minHeight: 480,
          fontSize: 12.5,
          border: "1px solid var(--border)",
          borderRadius: 6,
          overflow: "hidden",
        }}>

          {/* Left: file tree */}
          <div style={{ borderRight: "1px solid var(--border)", overflow: "auto", minHeight: 0, display: "flex", flexDirection: "column" }}>
            {/* Tree header: prefix filter + controls */}
            <div style={{ borderBottom: "1px solid var(--border)", padding: "8px 10px", flexShrink: 0, display: "flex", flexDirection: "column", gap: 6 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="mono muted" style={{ fontSize: 11, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {cid}
                </span>
                <button className="icon-btn" style={{ width: 22, height: 22, flexShrink: 0 }} title="Refresh" onClick={list.refetch}>
                  <Icon name="refresh" size={10} />
                </button>
              </div>
              <div className="input-icon" style={{ position: "relative" }}>
                <Icon name="filter" size={11} className="icon" />
                <input
                  className="input"
                  style={{ fontSize: 11.5, height: 26, paddingLeft: 24 }}
                  placeholder="Prefix filter… (Enter)"
                  value={prefix}
                  onChange={(e) => setPrefix(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") setAppliedPrefix(prefix.trim()); }}
                  onBlur={() => setAppliedPrefix(prefix.trim())}
                />
              </div>
              {appliedPrefix && (
                <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                  <span className="muted mono" style={{ fontSize: 10.5, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    prefix: {appliedPrefix}
                  </span>
                  <button className="icon-btn" style={{ width: 18, height: 18, flexShrink: 0 }} title="Clear filter" onClick={() => { setPrefix(""); setAppliedPrefix(""); }}>
                    <Icon name="x" size={9} />
                  </button>
                </div>
              )}
            </div>

            {/* Tree body */}
            <div style={{ flex: 1, overflow: "auto", padding: "6px 0" }}>
              {list.loading && !list.data ? (
                <div className="muted text-sm" style={{ padding: "12px 16px", textAlign: "center" }}>Loading…</div>
              ) : list.error ? (
                <div style={{ padding: "12px 16px", textAlign: "center" }}>
                  <span style={{ color: "var(--red)", fontSize: 12 }}>{list.error.title || list.error.message}</span>
                  <div style={{ marginTop: 6 }}>
                    <a onClick={list.refetch} style={{ cursor: "pointer", fontSize: 12 }}>Retry</a>
                  </div>
                </div>
              ) : (list.data?.documents ?? []).length === 0 ? (
                <div className="muted text-sm" style={{ padding: "12px 16px", textAlign: "center" }}>
                  {appliedPrefix
                    ? `No documents under "${appliedPrefix}".`
                    : "No documents in this collection yet."}
                </div>
              ) : (
                <KN_DocTreeNode
                  node={treeRoot}
                  depth={0}
                  openDirs={openDirs}
                  toggleDir={toggleDir}
                  selectedPath={selectedPath}
                  selectPath={(p) => { openPath(p); setViewMode("rendered"); }}
                />
              )}
            </div>
          </div>

          {/* Right: content pane */}
          <div style={{ display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0, minHeight: 0 }}>

            {/* Content pane header */}
            <div style={{ display: "flex", alignItems: "center", padding: "8px 12px", borderBottom: "1px solid var(--border)", gap: 8, flexShrink: 0, flexWrap: "wrap" }}>
              {creating ? (
                <span className="mono muted" style={{ fontSize: 12 }}>New document</span>
              ) : selectedPath ? (
                <>
                  <Icon name="doc" size={12} className="muted" />
                  <span className="mono" style={{ fontSize: 12, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {selectedPath}
                  </span>
                  {docMeta?.size != null && (
                    <span className="muted mono" style={{ fontSize: 10.5, flexShrink: 0 }}>
                      {docMeta.size < 1024 ? `${docMeta.size}B` : `${(docMeta.size / 1024).toFixed(1)}K`}
                    </span>
                  )}
                </>
              ) : (
                <span className="muted" style={{ fontSize: 12 }}>No document selected</span>
              )}

              {/* Actions in header */}
              <div style={{ marginLeft: "auto", display: "flex", gap: 6, flexShrink: 0 }}>
                {creating ? (
                  <>
                    <Btn size="sm" kind="primary" icon="plus" disabled={!newPath.trim() || save.loading} onClick={submitCreate}>
                      {save.loading ? "Creating…" : "Create"}
                    </Btn>
                    <Btn size="sm" kind="ghost" onClick={() => setCreating(false)} disabled={save.loading}>Cancel</Btn>
                  </>
                ) : editing ? (
                  <>
                    <Btn size="sm" kind="primary" icon="check" disabled={save.loading} onClick={submitEdit}>
                      {save.loading ? "Saving…" : "Save"}
                    </Btn>
                    <Btn size="sm" kind="ghost" onClick={() => {
                      setEditing(false);
                      setDraftTitle(docMeta?.title || "");
                      setDraftContent(doc.data?.content || "");
                    }} disabled={save.loading}>Discard</Btn>
                  </>
                ) : selectedPath ? (
                  <>
                    {isMarkdown && (
                      <Btn
                        size="sm"
                        kind="ghost"
                        onClick={() => setViewMode((m) => (m === "rendered" ? "raw" : "rendered"))}
                        title={viewMode === "rendered" ? "Show raw markdown source" : "Render the markdown"}
                      >
                        {viewMode === "rendered" ? "Raw" : "Rendered"}
                      </Btn>
                    )}
                    <Btn size="sm" kind="secondary" icon="edit" onClick={() => {
                      setDraftTitle(docMeta?.title || "");
                      setDraftContent(doc.data?.content || "");
                      setEditing(true);
                    }}>Edit</Btn>
                    <Btn size="sm" kind="ghost" icon="external" onClick={() => { setMoveTo(selectedPath); setMoving(true); }}>Move</Btn>
                    <Btn size="sm" kind="ghost" icon="trash" onClick={() => setDeleting(true)}>Delete</Btn>
                  </>
                ) : null}
              </div>
            </div>

            {/* Content pane body */}
            <div style={{ flex: 1, overflow: "auto", background: "var(--bg)", minHeight: 0 }}>
              {creating ? (
                <div style={{ padding: 16 }}>
                  <div className="col" style={{ gap: 10 }}>
                    <div className="muted text-sm mono" style={{ overflowWrap: "anywhere" }}>
                      PUT /v1/collections/{cid}/documents?path=
                    </div>
                    <div className="field">
                      <label className="field-label">Path</label>
                      <input
                        className="input mono"
                        value={newPath}
                        onChange={(e) => setNewPath(e.target.value)}
                        placeholder="concepts/slo.md"
                        style={{ width: "100%" }}
                        autoFocus
                      />
                      <div className="field-help">POSIX-like; no leading/trailing slash, no empty or '.'/'..' segments.</div>
                    </div>
                    <div className="field">
                      <label className="field-label">Title <span className="hint">optional; defaults to the path leaf</span></label>
                      <input
                        className="input"
                        value={draftTitle}
                        onChange={(e) => setDraftTitle(e.target.value)}
                        style={{ width: "100%" }}
                      />
                    </div>
                    <div className="field">
                      <label className="field-label">Content</label>
                      <textarea
                        className="textarea mono"
                        value={draftContent}
                        onChange={(e) => setDraftContent(e.target.value)}
                        style={{ width: "100%", minHeight: 300, fontSize: 12, lineHeight: 1.55 }}
                        placeholder="Document body. Stored in the content store and indexed for search."
                      />
                    </div>
                  </div>
                </div>
              ) : !selectedPath ? (
                <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>
                  Select a document to view its content.
                </div>
              ) : doc.loading && !doc.data ? (
                <div className="muted text-sm" style={{ padding: 24, textAlign: "center" }}>Loading…</div>
              ) : doc.error ? (
                <div style={{ padding: 16 }}>
                  <Banner kind="error" title={doc.error.title || "Failed to load document"} detail={doc.error.detail || doc.error.message} />
                </div>
              ) : editing ? (
                <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 10, height: "100%", boxSizing: "border-box" }}>
                  <div className="muted text-sm mono" style={{ overflowWrap: "anywhere" }}>
                    PUT /v1/collections/{cid}/documents?path={selectedPath}
                  </div>
                  <div className="field">
                    <label className="field-label">Title</label>
                    <input
                      className="input"
                      value={draftTitle}
                      onChange={(e) => setDraftTitle(e.target.value)}
                      style={{ width: "100%" }}
                    />
                  </div>
                  <div className="field" style={{ flex: 1, display: "flex", flexDirection: "column" }}>
                    <label className="field-label">Content</label>
                    <textarea
                      className="textarea mono"
                      value={draftContent}
                      onChange={(e) => setDraftContent(e.target.value)}
                      style={{ flex: 1, width: "100%", minHeight: 240, fontSize: 12, lineHeight: 1.55 }}
                    />
                  </div>
                </div>
              ) : isMarkdown && viewMode === "rendered" ? (
                <div
                  className="md-rendered"
                  style={{ padding: 16, fontSize: 13, lineHeight: 1.6, color: "var(--text)" }}
                >
                  {typeof window.renderMarkdown === "function"
                    ? window.renderMarkdown(doc.data?.content || "")
                    : (
                      <pre className="mono" style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12 }}>
                        {doc.data?.content || ""}
                      </pre>
                    )}
                </div>
              ) : (
                <pre className="mono" style={{ margin: 0, padding: 16, fontSize: 12, lineHeight: 1.55, color: "var(--text-2)", whiteSpace: "pre-wrap" }}>
                  {doc.data?.content || ""}
                </pre>
              )}
            </div>

            {/* Metadata strip at bottom when viewing a doc */}
            {!creating && !editing && selectedPath && docMeta && (
              <div style={{ borderTop: "1px solid var(--border)", padding: "6px 12px", display: "flex", gap: 16, flexShrink: 0, flexWrap: "wrap" }}>
                {docMeta.title && (
                  <span className="muted text-sm">
                    <span style={{ opacity: 0.6 }}>title</span>{" "}
                    <span>{docMeta.title}</span>
                  </span>
                )}
                {docMeta.id && (
                  <span className="muted text-sm mono" style={{ fontSize: 10.5 }}>
                    <span style={{ opacity: 0.6 }}>id</span>{" "}
                    <span>{docMeta.id}</span>
                  </span>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Delete confirm: nested modal on top of the browser */}
      {deleting && selectedPath && (
        <Modal
          title="Delete document"
          danger
          onClose={() => { if (!del.loading) setDeleting(false); }}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setDeleting(false)} disabled={del.loading}>Cancel</Btn>
              <Btn kind="danger" icon="trash" onClick={submitDelete} disabled={del.loading}>
                {del.loading ? "Deleting…" : "Delete"}
              </Btn>
            </>
          }
        >
          <div style={{ maxWidth: 460 }}>
            <p style={{ marginTop: 0 }}>
              Delete document at <span className="mono">{selectedPath}</span> from
              collection <span className="mono">{cid}</span>?
            </p>
            <p className="muted text-sm">
              This removes the entity row and its stored body. The action cannot be undone.
            </p>
          </div>
        </Modal>
      )}

      {/* Move / rename */}
      {moving && selectedPath && (
        <Modal
          title="Move / rename document"
          onClose={() => { if (!move.loading) setMoving(false); }}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setMoving(false)} disabled={move.loading}>Cancel</Btn>
              <Btn kind="primary" icon="check" disabled={move.loading || !moveTo.trim() || moveTo.trim() === selectedPath} onClick={submitMove}>
                {move.loading ? "Moving…" : "Move"}
              </Btn>
            </>
          }
        >
          <div style={{ maxWidth: 520 }}>
            <div className="muted text-sm mono mb-3" style={{ overflowWrap: "anywhere" }}>
              POST /v1/collections/{cid}/documents/move
            </div>
            <div className="field">
              <label className="field-label">From</label>
              <input className="input mono" value={selectedPath} disabled style={{ width: "100%", opacity: 0.6 }} />
            </div>
            <div className="field">
              <label className="field-label">To</label>
              <input
                className="input mono"
                value={moveTo}
                onChange={(e) => setMoveTo(e.target.value)}
                placeholder="new/path.md"
                style={{ width: "100%" }}
                autoFocus
              />
              <div className="field-help">Fails with 409 if the destination path is already occupied.</div>
            </div>
          </div>
        </Modal>
      )}
    </Modal>
  );
}

// Final path segment helper for the content view's title fallback display.
function _leafOf(path) {
  if (!path) return "";
  return path.split("/").pop();
}


// Modal: view all indexed chunks of a single document. Pulls from
// /collections/{id}/indexed_documents?document_id=X. A document whose
// row exists but has not been vectorised yet has no chunks; the modal
// says so rather than showing an empty void.
function KN_DocumentChunksModal({ collectionId, doc, onClose }) {
  const { useResource, apiFetch } = window.primerApi;
  const docId = doc.id;
  const chunks = useResource(
    `doc-chunks:${collectionId}:${docId}`,
    (signal) => apiFetch(
      "GET",
      `/collections/${encodeURIComponent(collectionId)}/indexed_documents?document_id=${encodeURIComponent(docId)}&limit=500`,
      null,
      { signal },
    ),
    { pollMs: null, deps: [collectionId, docId] },
  );
  const items = (chunks.data?.items || []).map((r) => ({
    document_id: r.document_id,
    chunk_id: r.chunk_id,
    text: r.text,
    meta: r.meta,
    score: null,
  }));
  const total = chunks.data?.total ?? null;

  return (
    <Modal
      title={
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Icon name="doc" size={13} className="muted" />
          <span>Chunks of <span className="mono">{doc.name || docId}</span></span>
        </span>
      }
      onClose={onClose}
      footer={
        <div style={{ display: "flex", alignItems: "center", gap: 12, flex: 1 }}>
          <span className="muted text-sm tabular">
            {total == null ? (chunks.loading ? "Loading…" : "—") : `${total} chunk${total === 1 ? "" : "s"}`}
          </span>
          <div style={{ marginLeft: "auto" }}>
            <Btn kind="ghost" onClick={onClose}>Close</Btn>
          </div>
        </div>
      }
    >
      <div style={{ width: "min(80vw, 880px)", maxWidth: "100%", minWidth: 0, overflowX: "hidden" }}>
        <div className="muted text-sm mb-3" style={{ overflowWrap: "anywhere" }}>
          <span className="mono">GET /v1/collections/{collectionId}/indexed_documents?document_id={docId}</span>
        </div>
        {chunks.loading && items.length === 0 && (
          <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
        )}
        {chunks.error && (
          <Banner kind="error" title={chunks.error.title || "Failed to load chunks"} detail={chunks.error.detail || chunks.error.message} />
        )}
        {!chunks.loading && items.length === 0 && !chunks.error && (
          <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>
            This document has no indexed chunks yet. Vector indexing runs
            separately from document ingestion; until it runs, the stored
            text is available on the document row but not chunked for search.
          </div>
        )}
        {items.length > 0 && (
          <div style={{ maxHeight: 480, overflow: "auto", overflowX: "hidden" }}>
            {items.map((entry, i) => (
              <KN_EntryRow key={i} entry={entry} index={i} />
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

function KN_NewCollectionModal({ embedProviders, sspProviders = [], cerProviders = [], pushToast, onClose, onCreate, existing }) {
  const isEdit = !!existing;
  const { useMutation, apiFetch } = window.primerApi;
  const [id, setId] = React.useState(existing?.id || "");
  const [description, setDescription] = React.useState(existing?.description || "");
  const [providerId, setProviderId] = React.useState(existing?.embedder?.provider_id || "");
  const [model, setModel] = React.useState(existing?.embedder?.model || "");
  const [searchProviderId, setSearchProviderId] = React.useState(existing?.search_provider_id || "");
  const [fieldErrors, setFieldErrors] = React.useState({});

  // MMR state
  const [mmrEnabled, setMmrEnabled] = React.useState(!!(existing?.search?.mmr));
  const [mmrLambda, setMmrLambda] = React.useState(
    existing?.search?.mmr?.lambda_mult != null ? String(existing.search.mmr.lambda_mult) : "0.5"
  );
  const [mmrFetchK, setMmrFetchK] = React.useState(
    existing?.search?.mmr?.fetch_k != null ? String(existing.search.mmr.fetch_k) : ""
  );

  // Cross-encoder reranker (CER) state
  const [cerEnabled, setCerEnabled] = React.useState(!!(existing?.search?.cer));
  const [cerProviderId, setCerProviderId] = React.useState(existing?.search?.cer?.provider_id || "");
  const [cerModel, setCerModel] = React.useState(existing?.search?.cer?.model || "");
  const [cerTopN, setCerTopN] = React.useState(
    existing?.search?.cer?.top_n != null ? String(existing.search.cer.top_n) : "100"
  );

  React.useEffect(() => {
    if (!providerId && embedProviders.length > 0) setProviderId(embedProviders[0].id);
  }, [embedProviders, providerId]);
  React.useEffect(() => {
    if (!searchProviderId && sspProviders.length > 0) setSearchProviderId(sspProviders[0].id);
  }, [sspProviders, searchProviderId]);
  React.useEffect(() => {
    if (cerEnabled && !cerProviderId && cerProviders.length > 0) setCerProviderId(cerProviders[0].id);
  }, [cerProviders, cerProviderId, cerEnabled]);

  // Model options come from the selected provider's row (T0025 — no live
  // introspection; the provider stores its declared model list).
  const selectedProvider = embedProviders.find((p) => p.id === providerId);
  const modelOptions = selectedProvider?.models ?? [];
  React.useEffect(() => {
    if (modelOptions.length > 0 && !modelOptions.some((m) => m.name === model)) {
      setModel(modelOptions[0].name);
    }
  }, [modelOptions]);  // eslint-disable-line react-hooks/exhaustive-deps

  const selectedCerProvider = cerProviders.find((p) => p.id === cerProviderId);
  const cerModelOptions = selectedCerProvider?.models ?? [];
  React.useEffect(() => {
    if (cerEnabled && cerModelOptions.length > 0 && !cerModelOptions.some((m) => m.name === cerModel)) {
      setCerModel(cerModelOptions[0].name);
    }
  }, [cerModelOptions, cerEnabled]);  // eslint-disable-line react-hooks/exhaustive-deps

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
    // Build search config
    let search = null;
    const mmrPart = mmrEnabled ? {
      lambda_mult: parseFloat(mmrLambda) || 0.5,
      fetch_k: mmrFetchK ? (parseInt(mmrFetchK, 10) || null) : null,
    } : null;
    const cerPart = cerEnabled && cerProviderId && cerModel ? {
      provider_id: cerProviderId,
      model: cerModel,
      top_n: parseInt(cerTopN, 10) || 100,
    } : null;
    if (mmrPart || cerPart) {
      search = {};
      if (mmrPart) search.mmr = mmrPart;
      if (cerPart) search.cer = cerPart;
    }
    const body = {
      ...(isEdit ? { id: existing.id } : (id ? { id } : {})),
      description: description || null,
      embedder: { provider_id: providerId, model },
      search_provider_id: searchProviderId,
      search,
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
        <label className="field-label">
          Embedding provider
          {isEdit && <span className="hint">locked after create</span>}
        </label>
        {isEdit ? (
          <input
            className="input"
            value={providerId}
            disabled
            style={{ width: "100%", opacity: 0.6 }}
          />
        ) : (
          <select
            className="select"
            value={providerId}
            onChange={(e) => setProviderId(e.target.value)}
            style={{ width: "100%" }}
          >
            <option value="">-- pick a provider --</option>
            {embedProviders.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
          </select>
        )}
        {!isEdit && embedProviders.length === 0 && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No embedding providers configured. Create one at /providers/embedding first.
          </div>
        )}
        {fieldErrors["body.embedder.provider_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.embedder.provider_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">
          Embedding model
          {isEdit && <span className="hint">locked after create</span>}
        </label>
        {isEdit ? (
          <input
            className="input"
            value={model}
            disabled
            style={{ width: "100%", opacity: 0.6 }}
          />
        ) : (
          <select
            className="select"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            style={{ width: "100%" }}
          >
            <option value="">-- pick a model --</option>
            {modelOptions.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
          </select>
        )}
        {!isEdit && <div className="field-help">Model list comes from the provider row, not a live introspection (T0025).</div>}
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

      {/* MMR (Maximal Marginal Relevance) */}
      <div className="field">
        <label className="field-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="checkbox"
            checked={mmrEnabled}
            onChange={(e) => setMmrEnabled(e.target.checked)}
          />
          MMR diversification
          <span className="hint">optional</span>
        </label>
        <div className="field-help">
          Maximal Marginal Relevance re-ranks results to reduce near-duplicate chunks.
        </div>
        {mmrEnabled && (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 8 }}>
            <div>
              <label className="field-label" style={{ fontSize: 11 }}>lambda_mult (0-1)</label>
              <input
                className="input"
                type="number"
                min="0"
                max="1"
                step="0.05"
                value={mmrLambda}
                onChange={(e) => setMmrLambda(e.target.value)}
                style={{ width: "100%" }}
              />
              <div className="field-help" style={{ fontSize: 10.5 }}>
                1.0 = pure relevance, 0.0 = pure diversity. Default 0.5.
              </div>
              {fieldErrors["body.search.mmr.lambda_mult"] && (
                <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.search.mmr.lambda_mult"]}</div>
              )}
            </div>
            <div>
              <label className="field-label" style={{ fontSize: 11 }}>fetch_k (optional)</label>
              <input
                className="input"
                type="number"
                min="1"
                value={mmrFetchK}
                onChange={(e) => setMmrFetchK(e.target.value)}
                placeholder="auto"
                style={{ width: "100%" }}
              />
              <div className="field-help" style={{ fontSize: 10.5 }}>
                Candidates fetched before MMR runs. Leave blank to auto-compute (max(50, 10*k)).
              </div>
              {fieldErrors["body.search.mmr.fetch_k"] && (
                <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.search.mmr.fetch_k"]}</div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Cross-encoder reranker (CER) */}
      <div className="field">
        <label className="field-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="checkbox"
            checked={cerEnabled}
            onChange={(e) => setCerEnabled(e.target.checked)}
          />
          Cross-encoder reranker
          <span className="hint">optional</span>
        </label>
        <div className="field-help">
          Re-scores retrieved candidates with a cross-encoder model before returning results.
        </div>
        {cerEnabled && (
          <div style={{ marginTop: 8 }}>
            <div className="field" style={{ marginBottom: 8 }}>
              <label className="field-label" style={{ fontSize: 11 }}>Cross-encoder provider</label>
              <select
                className="select"
                value={cerProviderId}
                onChange={(e) => setCerProviderId(e.target.value)}
                style={{ width: "100%" }}
              >
                <option value="">-- pick a provider --</option>
                {cerProviders.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
              </select>
              {cerProviders.length === 0 && (
                <div className="field-help" style={{ color: "var(--amber)", fontSize: 10.5 }}>
                  No cross-encoder providers configured. Create one at /providers/cross-encoder first.
                </div>
              )}
              {fieldErrors["body.search.cer.provider_id"] && (
                <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.search.cer.provider_id"]}</div>
              )}
            </div>
            <div className="field" style={{ marginBottom: 8 }}>
              <label className="field-label" style={{ fontSize: 11 }}>Cross-encoder model</label>
              <select
                className="select"
                value={cerModel}
                onChange={(e) => setCerModel(e.target.value)}
                style={{ width: "100%" }}
              >
                <option value="">-- pick a model --</option>
                {cerModelOptions.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
              </select>
              {fieldErrors["body.search.cer.model"] && (
                <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.search.cer.model"]}</div>
              )}
            </div>
            <div>
              <label className="field-label" style={{ fontSize: 11 }}>top_n</label>
              <input
                className="input"
                type="number"
                min="1"
                value={cerTopN}
                onChange={(e) => setCerTopN(e.target.value)}
                style={{ width: "100%", maxWidth: 160 }}
              />
              <div className="field-help" style={{ fontSize: 10.5 }}>
                Candidates the cross-encoder scores. Default 100; quality plateaus past ~100.
              </div>
              {fieldErrors["body.search.cer.top_n"] && (
                <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.search.cer.top_n"]}</div>
              )}
            </div>
          </div>
        )}
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
  // A non-system collection's documents are path-addressed: the
  // per-collection list route returns {documents:[{path, document_id,
  // size}]} with no pagination and no name/meta columns, and the bodies
  // are reached by ?path=. The legacy storage-row table below can't
  // represent that shape, so for a selected user collection we defer to
  // the path-addressed browser (full list + view/edit/move/delete).
  const isPathCollection = !!collectionFilter && !!selectedCollection && !isSystemFilter;

  // Pagination — keep prev/next driven by offset. Reset when the
  // collection filter changes so we don't end up past-the-end after a
  // switch from a large collection to a small one.
  const PAGE_SIZE = 50;
  const [offset, setOffset] = React.useState(0);
  React.useEffect(() => { setOffset(0); }, [collectionFilter, isSystemFilter]);

  const list = useResource(
    `documents:list:${collectionFilter}:${isSystemFilter ? "vec" : "store"}:${offset}`,
    (signal) => isPathCollection
      // Path-addressed user collection: handled by the inline browser
      // below, so skip the (incompatible) storage-row enumeration.
      ? Promise.resolve(null)
      : apiFetch(
          "GET",
          collectionFilter
            ? (isSystemFilter
                ? `/collections/${encodeURIComponent(collectionFilter)}/indexed_documents?limit=${PAGE_SIZE}&offset=${offset}`
                : `/collections/${encodeURIComponent(collectionFilter)}/documents?limit=${PAGE_SIZE}&offset=${offset}`)
            : `/documents?limit=${PAGE_SIZE}&offset=${offset}`,
          null,
          { signal },
        ),
    { pollMs: null, deps: [collectionFilter, isSystemFilter, isPathCollection, offset] },
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
  const [viewingChunks, setViewingChunks] = React.useState(null);
  const [deleting, setDeleting] = React.useState(null);

  const del = window.primerApi.useMutation(
    (docId) => apiFetch("DELETE", "/documents/" + encodeURIComponent(docId)),
    {
      onSuccess: () => {
        const removed = deleting?.id;
        setDeleting(null);
        if (typeof pushToast === "function") {
          pushToast({ kind: "success", title: "Document deleted", detail: removed || "" });
        }
        list.refetch();
      },
      onError: (err) => {
        if (typeof pushToast === "function") {
          pushToast({ kind: "error", title: err?.title || "Delete failed", detail: err?.detail || err?.message, requestId: err?.requestId });
        }
      },
    },
  );

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

  // A selected user collection is path-addressed: render the full
  // path-browser (list + view/edit/move/delete) instead of the legacy
  // storage-row table, which can't consume the {documents:[...]} shape.
  if (isPathCollection) {
    return (
      <KN_CollectionDocBrowserModal
        collection={selectedCollection}
        pushToast={pushToast}
        onClose={() => setCollectionFilter("")}
      />
    );
  }

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
        <div style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center" }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          {isSystemFilter ? (
            <span className="muted text-sm" title="System collections are maintained automatically by their internal subsystem; documents cannot be ingested by hand.">
              System-managed (read-only)
            </span>
          ) : (
            <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>Ingest document</Btn>
          )}
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
              onClick={() => setViewingChunks({ collectionId: d.collection_id, doc: d })}
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
                  <td className="mono">
                    <a
                      style={{ cursor: "pointer", color: "var(--accent)" }}
                      title="View this document's indexed chunks"
                      onClick={() => setViewingChunks({ collectionId: d.collection_id, doc: d })}
                    >{d.id}</a>
                  </td>
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
                  <td style={{ textAlign: "right", paddingRight: 12, whiteSpace: "nowrap" }}>
                    <Btn size="sm" kind="ghost" icon="list" onClick={() => setViewingChunks({ collectionId: d.collection_id, doc: d })} title="View chunks" />
                    {!d._indexed && (
                      <Btn size="sm" kind="ghost" icon="edit" onClick={() => setEditing(d)} title="Edit document" />
                    )}
                    {!d._indexed && (
                      <Btn size="sm" kind="ghost" icon="trash" onClick={() => setDeleting(d)} title="Delete document" />
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
          onBatchDone={() => { setCreateOpen(false); list.refetch(); }}
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
      {viewingChunks && (
        <KN_DocumentChunksModal
          collectionId={viewingChunks.collectionId}
          doc={viewingChunks.doc}
          onClose={() => setViewingChunks(null)}
        />
      )}
      {deleting && (
        <Modal
          title="Delete document"
          onClose={() => { if (!del.loading) setDeleting(null); }}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setDeleting(null)} disabled={del.loading}>Cancel</Btn>
              <Btn kind="danger" icon="trash" onClick={() => del.mutate(deleting.id)} disabled={del.loading}>
                {del.loading ? "Deleting…" : "Delete"}
              </Btn>
            </>
          }
        >
          <div style={{ maxWidth: 460 }}>
            <p style={{ marginTop: 0 }}>
              Delete document <span className="mono">{deleting.id}</span>
              {deleting.name ? <> (<span className="mono">{deleting.name}</span>)</> : null} from
              collection <span className="mono">{deleting.collection_id}</span>?
            </p>
            <p className="muted text-sm">
              This removes the document storage row. Any vector chunks
              already indexed for it are not pruned by this action.
            </p>
          </div>
        </Modal>
      )}
    </div>
  );
}

function KN_NewDocumentModal({ collections, defaultCollection, pushToast, onClose, onCreate, onBatchDone, existing }) {
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

  // Batch ingest: dropping or selecting MORE THAN ONE file creates one
  // document per file directly (convert -> POST /documents), bypassing
  // the single-file textarea edit. batch is null when not batching, or
  // { files: [{name, status, error}], done } while/after running.
  const [batch, setBatch] = React.useState(null);

  const processBatchFiles = async (fileList) => {
    const arr = Array.from(fileList);
    if (!collectionId) {
      setConvertError("Pick a collection before uploading multiple files.");
      return;
    }
    setConvertError(null);
    setBatch({ files: arr.map((f) => ({ name: f.name, status: "pending" })), done: false });
    const setStatus = (i, status, error) =>
      setBatch((b) => b && ({ ...b, files: b.files.map((x, j) => j === i ? { ...x, status, error } : x) }));
    let created = 0;
    for (let i = 0; i < arr.length; i++) {
      setStatus(i, "converting");
      try {
        const fd = new FormData();
        fd.append("file", arr[i]);
        const conv = await apiFetch("POST", "/documents/_convert_file", fd);
        // User collections are path-addressed: upsert through the same
        // PUT /collections/{id}/documents?path= contract the single-file
        // flow uses, with the converted filename as the path. The earlier
        // POST /documents shape omitted `path` (and put the body in
        // `meta.text`), so every file 422'd with "Missing or invalid: path".
        const docPath = conv.filename || arr[i].name;
        await apiFetch(
          "PUT",
          `/collections/${encodeURIComponent(collectionId)}/documents` +
            `?path=${encodeURIComponent(docPath)}`,
          { content: conv.text || "", title: conv.filename || arr[i].name },
        );
        created += 1;
        setStatus(i, "done");
      } catch (err) {
        setStatus(i, "error", (err && (err.detail || err.message)) || "Failed");
      }
    }
    setBatch((b) => b && ({ ...b, done: true }));
    if (created > 0 && typeof pushToast === "function") {
      pushToast({ kind: "success", title: `Ingested ${created} document${created === 1 ? "" : "s"}`, detail: collectionId });
    }
  };

  // Route a FileList to the right handler: one file uses the existing
  // convert-to-textarea edit flow; multiple files batch-ingest.
  const handleFiles = (fileList) => {
    if (!fileList || fileList.length === 0) return;
    if (fileList.length === 1) handleConvertFile(fileList[0]);
    else processBatchFiles(fileList);
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
    handleFiles(e.dataTransfer?.files);
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

  // Batch mode renders a dedicated progress view instead of the form.
  if (batch) {
    const allDone = batch.done;
    const okCount = batch.files.filter((f) => f.status === "done").length;
    const errCount = batch.files.filter((f) => f.status === "error").length;
    return (
      <Modal
        title={`Ingesting ${batch.files.length} files into ${collectionId}`}
        onClose={allDone ? () => (onBatchDone ? onBatchDone() : onClose()) : undefined}
        footer={
          <Btn
            kind="primary"
            icon="check"
            disabled={!allDone}
            onClick={() => (onBatchDone ? onBatchDone() : onClose())}
          >{allDone ? `Done (${okCount} created${errCount ? `, ${errCount} failed` : ""})` : "Working…"}</Btn>
        }
      >
        <div style={{ width: "min(70vw, 560px)", maxWidth: "100%" }}>
          {batch.files.map((f, i) => (
            <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 0", borderTop: i ? "1px solid var(--border)" : "none" }}>
              <span style={{ width: 18, textAlign: "center" }}>
                {f.status === "done" ? <Icon name="check-circle" size={13} style={{ color: "var(--green)" }} />
                  : f.status === "error" ? <Icon name="x-circle" size={13} style={{ color: "var(--red)" }} />
                  : f.status === "converting" ? <Icon name="zap" size={13} style={{ color: "var(--accent)" }} />
                  : <span className="muted">·</span>}
              </span>
              <span className="mono text-sm" style={{ flex: 1, minWidth: 0, overflowWrap: "anywhere" }}>{f.name}</span>
              <span className="muted text-sm">
                {f.status === "converting" ? "converting…" : f.status === "done" ? "created" : f.status === "error" ? (f.error || "failed") : "queued"}
              </span>
            </div>
          ))}
        </div>
      </Modal>
    );
  }

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
          {collections.filter((c) => !c.system).map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
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
                  {isDragOver ? "Release to upload" : "Drag and drop one or more files here"}
                </div>
                <div className="muted text-sm">or</div>
                <label
                  className="btn"
                  style={{ cursor: "pointer", fontSize: 12 }}
                  title="PDF, DOCX, PPTX, XLSX, HTML, .md, .txt, images, ... - text formats are stored as-is; docling converts binary formats to markdown. Select multiple files to ingest each as a separate document."
                >
                  <input
                    type="file"
                    multiple
                    accept=".pdf,.docx,.pptx,.xlsx,.html,.htm,.md,.markdown,.txt,.png,.jpg,.jpeg"
                    style={{ display: "none" }}
                    onChange={(e) => {
                      handleFiles(e.target.files);
                      e.target.value = "";
                    }}
                  />
                  Choose file(s)
                </label>
                <div className="muted text-sm" style={{ marginTop: 6, fontSize: 11 }}>
                  PDF · DOCX · PPTX · XLSX · HTML · .md · .txt · images · ≤ 32 MB · multiple = one document each
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
