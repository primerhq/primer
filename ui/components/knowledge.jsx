/* global React, Icon, Btn, StatusPill, Banner, relativeTime */

const COLLECTIONS = [
  { id: "docs-public", search_provider_id: "pgvector-prod", desc: "Public-facing API docs", embedding_provider: "openai-emb-1", model: "text-embedding-3-large", docs: 1842, chunks: 18204, last_ingest: 540 },
  { id: "internal-runbooks", search_provider_id: "pgvector-prod", desc: "On-call runbooks", embedding_provider: "openai-emb-1", model: "text-embedding-3-large", docs: 312, chunks: 4128, last_ingest: 3600 * 8 },
  { id: "support-tickets-2024", search_provider_id: "pgvectorscale-archive", desc: "Closed tickets archive", embedding_provider: "voyage-1", model: "voyage-3", docs: 18420, chunks: 184200, last_ingest: 3600 * 24 },
  { id: "code-symbols", search_provider_id: "pgvector-prod", desc: "Indexed source symbols", embedding_provider: "openai-emb-1", model: "text-embedding-3-small", docs: 8412, chunks: 28412, last_ingest: 120 },
];

const DOCUMENTS = [
  { id: "doc-7f3a9c2b", collection: "docs-public", name: "api-reference.md", preview: "The Matrix API is organized around REST. All requests…", ingested: 540, status: "ok" },
  { id: "doc-1c4d8b7e", collection: "docs-public", name: "quickstart.md", preview: "Spin up an agent in 5 minutes. Install with `pip install matrix-cli`…", ingested: 540, status: "ok" },
  { id: "doc-9b2e6f1a", collection: "internal-runbooks", name: "on-call-rotation.md", preview: "If a worker pool drops below 50% capacity, the rotation triggers PD…", ingested: 3600 * 8, status: "ok" },
  { id: "doc-4e8c2a1d", collection: "internal-runbooks", name: "incident-template.md", preview: "## Summary\\n## Impact\\n## Root cause\\n## Action items…", ingested: 3600 * 8, status: "ok" },
  { id: "doc-2a6d4f8b", collection: "support-tickets-2024", name: "ticket-48201.txt", preview: "Customer reports double-billing on the Pro plan. Charge ch_3OZ…", ingested: 3600 * 24, status: "ok" },
  { id: "doc-6c3f9a2b", collection: "code-symbols", name: "scheduler.py:claim_session", preview: "def claim_session(self, worker_id: str) -> Session | None:…", ingested: 120, status: "ok" },
  { id: "doc-8d4a1f3b", collection: "docs-public", name: "predicates.md", preview: "Predicates are JSON trees evaluated server-side. Each node is either a clause or a group.", ingested: 540, status: "ok" },
  { id: "doc-5b9e3c8a", collection: "code-symbols", name: "scheduler.py:assign_worker", preview: "def assign_worker(self, session: Session) -> str | None:…", ingested: 120, status: "pending" },
];

function CollectionsPage({ onOpen, ssps, ssmState, onNavigate, onSearchCollection }) {
  const [sel, setSel] = React.useState(null);
  const [sspFilter, setSspFilter] = React.useState("");
  const selected = sel ? COLLECTIONS.find((c) => c.id === sel) : null;

  const filtered = sspFilter ? COLLECTIONS.filter((c) => c.search_provider_id === sspFilter) : COLLECTIONS;
  const noSSPs = ssmState === "none" || ssps.length === 0;

  // Map of known SSPs for stale-check
  const knownSSPs = new Set(ssps.map((p) => p.id));

  return (
    <div className="col" style={{ gap: 14 }}>
      {noSSPs && (
        <Banner
          kind="warning"
          title="No Semantic Search providers configured"
          detail="Collections require a SemanticSearchProvider to back their vector index. Configure one before creating a collection."
          actions={<Btn size="sm" kind="primary" icon="plus" onClick={() => onNavigate && onNavigate("semantic-search")}>Create one</Btn>}
        />
      )}
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter collections…" />
        </div>
        <div className="sep-v" />
        <select className="select" value={sspFilter} onChange={(e) => setSspFilter(e.target.value)}>
          <option value="">all search providers</option>
          {ssps.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" disabled={noSSPs} title={noSSPs ? "Configure a Semantic Search provider first" : ""}>New collection</Btn>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: selected ? "1.6fr 1fr" : "1fr", gap: 18 }}>
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>ID</th>
                <th>Embedder</th>
                <th>Search provider</th>
                <th style={{ textAlign: "right" }}>Docs</th>
                <th style={{ textAlign: "right" }}>Chunks</th>
                <th>Last ingest</th>
                <th style={{ width: 40 }}></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((c) => {
                const stale = !knownSSPs.has(c.search_provider_id);
                return (
                  <tr key={c.id} className={sel === c.id ? "selected" : ""} onClick={() => setSel(sel === c.id ? null : c.id)}>
                    <td className="mono">
                      {c.id}
                      {stale && (
                        <span className="pill pill-paused" style={{ marginLeft: 8, fontSize: 9.5 }}>
                          <span className="dot"></span>stale: SSP deleted
                        </span>
                      )}
                    </td>
                    <td className="mono muted text-sm">{c.embedding_provider} <span style={{ color: "var(--text-4)" }}>· {c.model}</span></td>
                    <td className="mono text-sm">
                      <a
                        style={{ color: stale ? "var(--red)" : "var(--accent)", cursor: "pointer" }}
                        onClick={(e) => { e.stopPropagation(); onNavigate && !stale && onNavigate("ssp-detail", c.search_provider_id); }}
                      >
                        {c.search_provider_id}
                      </a>
                    </td>
                    <td className="mono num tabular">{c.docs.toLocaleString()}</td>
                    <td className="mono num tabular muted">{c.chunks.toLocaleString()}</td>
                    <td className="mono muted">{relativeTime(c.last_ingest)}</td>
                    <td onClick={(e) => e.stopPropagation()} style={{ textAlign: "right", paddingRight: 10 }}>
                      <button
                        className="icon-btn"
                        style={{ width: 26, height: 26 }}
                        title={stale ? "Provider deleted — search disabled" : `Search this collection`}
                        disabled={stale}
                        onClick={() => !stale && onSearchCollection && onSearchCollection(c.id)}
                      >
                        <Icon name="search" size={12} />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {selected && <CollectionDetail c={selected} onOpenDocs={() => onOpen(selected.id)} onNavigate={onNavigate} ssps={ssps} onSearchCollection={onSearchCollection} />}
      </div>
    </div>
  );
}

function CollectionDetail({ c, onOpenDocs, onNavigate, ssps, onSearchCollection }) {
  const stale = !ssps.some((p) => p.id === c.search_provider_id);
  return (
    <div className="panel">
      <div className="panel-h">
        <Icon name="collection" size={13} className="muted" />
        <span className="mono">{c.id}</span>
      </div>
      <div className="panel-body">
        {stale && (
          <Banner
            kind="error"
            title="Search provider deleted"
            detail={`This collection references SSP "${c.search_provider_id}" which no longer exists. The collection can't be searched until it's reassigned (no operator action available in v1 — pending reindex feature).`}
          />
        )}
        <div className="kv" style={{ gridTemplateColumns: "140px 1fr" }}>
          <dt>search provider</dt>
          <dd>
            <a
              className="mono"
              style={{ color: stale ? "var(--red)" : "var(--accent)", cursor: "pointer" }}
              onClick={() => onNavigate && !stale && onNavigate("ssp-detail", c.search_provider_id)}
            >
              {c.search_provider_id}{!stale && " →"}
            </a>
          </dd>
          <dt>embedding</dt><dd>{c.embedding_provider}</dd>
          <dt>model</dt><dd>{c.model}</dd>
          <dt>docs</dt><dd>{c.docs.toLocaleString()}</dd>
          <dt>chunks</dt><dd>{c.chunks.toLocaleString()}</dd>
          <dt>last ingest</dt><dd>{relativeTime(c.last_ingest)}</dd>
        </div>
        <div className="mt-3" style={{ display: "flex", gap: 6 }}>
          <Btn size="sm" kind="primary" icon="search" onClick={() => onSearchCollection && onSearchCollection(c.id)} disabled={stale}>Search this collection</Btn>
          <Btn size="sm" kind="ghost" icon="doc" onClick={onOpenDocs} disabled={stale}>Documents</Btn>
          <Btn size="sm" kind="ghost" icon="refresh" disabled={stale}>Re-ingest</Btn>
        </div>
      </div>
    </div>
  );
}

function DocumentsPage({ filterCollection, onClearFilter }) {
  const [filter, setFilter] = React.useState(filterCollection || "");
  React.useEffect(() => { setFilter(filterCollection || ""); }, [filterCollection]);
  const filtered = DOCUMENTS.filter((d) => !filter || d.collection === filter);

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter documents…" />
        </div>
        <div className="sep-v" />
        <select className="select" value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">all collections</option>
          {COLLECTIONS.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
        </select>
        {filter && (
          <Btn size="sm" kind="ghost" icon="x" onClick={() => { setFilter(""); onClearFilter && onClearFilter(); }}>Clear</Btn>
        )}
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus">Ingest document</Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Collection</th>
              <th>Name</th>
              <th>Preview</th>
              <th>Status</th>
              <th>Ingested</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((d) => (
              <tr key={d.id}>
                <td className="mono">{d.id}</td>
                <td className="mono muted text-sm">{d.collection}</td>
                <td className="mono">{d.name}</td>
                <td className="muted" style={{ maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 11.5 }}>{d.preview}</td>
                <td>
                  {d.status === "ok" ? <span className="pill pill-ended"><span className="dot"></span>indexed</span> : <span className="pill pill-paused"><span className="dot"></span>pending</span>}
                </td>
                <td className="mono muted">{relativeTime(d.ingested)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Search test bench
// ----------------------------------------------------------------------

function SearchBench({ subsystemOn, collectionId }) {
  const [query, setQuery] = React.useState("how does the scheduler claim a session?");
  const [target, setTarget] = React.useState(collectionId ? "collections" : "collections");
  const [collection, setCollection] = React.useState(collectionId || "docs-public");
  const [topK, setTopK] = React.useState(5);
  const [rerank, setRerank] = React.useState(true);
  const [latency, setLatency] = React.useState(0);
  const [results, setResults] = React.useState(null);
  const [running, setRunning] = React.useState(false);

  // When scoped to a specific collection, lock the target + collection
  const isScoped = !!collectionId;
  React.useEffect(() => { if (collectionId) setCollection(collectionId); }, [collectionId]);

  const run = () => {
    if (!subsystemOn) return;
    setRunning(true);
    setResults(null);
    setTimeout(() => {
      setLatency(Math.floor(Math.random() * 80 + 38));
      setResults(generateResults(target, query, topK));
      setRunning(false);
    }, 380);
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      {!subsystemOn && (
        <Banner
          kind="error"
          title="Internal Collections subsystem is OFF"
          detail="All search routes return 503 until the subsystem is configured and bootstrapped."
          actions={<Btn size="sm" kind="primary" icon="settings">Configure</Btn>}
        />
      )}

      <div className="panel">
        <div className="panel-h">
          <Icon name="search" size={13} className="muted" />
          <span>Query</span>
          <span className="sub">· POST /v1/{target}/search</span>
          <div className="right">
            <span className={`mono text-sm ${subsystemOn ? "" : "muted"}`} style={{ color: subsystemOn ? "var(--green)" : "var(--text-3)" }}>
              ● {subsystemOn ? "subsystem ON" : "subsystem OFF"}
            </span>
          </div>
        </div>
        <div className="panel-body">
          {!isScoped && (
            <div className="chip-group" style={{ marginBottom: 10 }}>
              {["agents", "graphs", "collections", "tools"].map((t) => (
                <span key={t} className={`chip ${target === t ? "active" : ""}`} onClick={() => setTarget(t)}>
                  <Icon name={t === "agents" ? "agent" : t === "graphs" ? "graph" : t === "collections" ? "collection" : "tools"} size={11} />
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
              placeholder="Natural-language query…"
            />
            <Btn kind="primary" icon="search" disabled={!subsystemOn || running} onClick={run}>Search</Btn>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 14, marginTop: 10, fontSize: 12 }}>
            {target === "collections" && !isScoped && (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="muted">collection</span>
                <select className="select" value={collection} onChange={(e) => setCollection(e.target.value)}>
                  {COLLECTIONS.map((c) => <option key={c.id} value={c.id}>{c.id}</option>)}
                </select>
              </div>
            )}
            {isScoped && (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span className="muted">collection</span>
                <span className="mono" style={{ color: "var(--text)" }}>{collection}</span>
              </div>
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className="muted">top_k</span>
              <input className="input" type="number" value={topK} onChange={(e) => setTopK(Math.max(1, +e.target.value))} style={{ width: 60 }} />
            </div>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input type="checkbox" checked={rerank} onChange={(e) => setRerank(e.target.checked)} />
              <span className="muted">cross-encoder rerank</span>
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input type="checkbox" defaultChecked />
              <span className="muted">MMR</span>
            </label>
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
              <span className="sub">· {results.length} hits</span>
              <span className="sub">· <span className="mono" style={{ color: "var(--accent)" }}>{latency}ms</span></span>
            </>
          )}
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {running ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--text-3)" }}>
              <Icon name="zap" size={18} style={{ color: "var(--accent)" }} />
              <div className="mt-2">Embedding query & running cosine search…</div>
            </div>
          ) : !results ? (
            <div style={{ padding: 36, textAlign: "center", color: "var(--text-4)", fontSize: 13 }}>
              Hit <kbd style={{ background: "var(--bg-2)", border: "1px solid var(--border)", padding: "1px 5px", borderRadius: 4, fontFamily: "IBM Plex Mono" }}>Search</kbd> to query.
            </div>
          ) : (
            <div>
              {results.map((r, i) => (
                <SearchResult key={i} r={r} rank={i + 1} />
              ))}
            </div>
          )}
        </div>
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
          <div className="mono" style={{ fontSize: 12.5, fontWeight: 500 }}>{r.id}</div>
          <div className="muted text-sm mono" style={{ fontSize: 11.5 }}>{r.collection} · {r.name}</div>
        </div>
        <div>
          <div className="mono tabular" style={{ fontSize: 14, fontWeight: 600, color: "var(--accent)", textAlign: "right" }}>{r.score.toFixed(3)}</div>
          <div className="muted text-sm mono" style={{ fontSize: 10.5, textAlign: "right" }}>cosine</div>
        </div>
        <button className="icon-btn" onClick={() => setOpen(!open)}>
          <Icon name={open ? "chevron-down" : "chevron-right"} size={11} />
        </button>
      </div>
      <div style={{ marginTop: 8, paddingLeft: 34, fontSize: 12, color: "var(--text-2)", lineHeight: 1.5 }}>
        <Highlight text={r.snippet} />
      </div>
      {open && (
        <div style={{ paddingLeft: 34, marginTop: 8 }}>
          <div className="muted text-sm mono mb-2">extensions:</div>
          <div className="code-block">{JSON.stringify({ chunk_idx: r.chunk_idx, embedding_provider: "openai-emb-1", reranker_score: r.rerankerScore }, null, 2)}</div>
        </div>
      )}
    </div>
  );
}

function Highlight({ text }) {
  // Highlight quoted words
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((p, i) => p.startsWith("**") ? (
    <span key={i} style={{ background: "var(--accent-dim)", color: "var(--accent)", padding: "0 2px", borderRadius: 2 }}>{p.slice(2, -2)}</span>
  ) : <span key={i}>{p}</span>);
}

function generateResults(target, query, topK) {
  const corpus = {
    collections: [
      { id: "doc-6c3f9a2b#3", collection: "code-symbols", name: "scheduler.py:claim_session", snippet: "def **claim_session**(self, worker_id: str) -> Session | None:\n    \"\"\"Atomically claim the next eligible session for this worker.\"\"\"\n    with self._db.transaction() as cur:\n        row = cur.fetchone('SELECT id FROM sessions WHERE status=...')", score: 0.892, chunk_idx: 3, rerankerScore: 0.94 },
      { id: "doc-7f3a9c2b#12", collection: "docs-public", name: "api-reference.md", snippet: "## Scheduler\n\nThe **scheduler** dispatches sessions to workers based on capacity. Each call to **claim** atomically reserves a session and returns it to the caller.", score: 0.812, chunk_idx: 12, rerankerScore: 0.88 },
      { id: "doc-9b2e6f1a#1", collection: "internal-runbooks", name: "on-call-rotation.md", snippet: "If the **scheduler** fails to **claim** a session within 30s, the on-call rotation is paged via PagerDuty.", score: 0.748, chunk_idx: 1, rerankerScore: 0.81 },
      { id: "doc-7f3a9c2b#28", collection: "docs-public", name: "api-reference.md", snippet: "**Claiming** a **session** requires a registered worker. The worker_id is propagated to the session row via UPDATE…RETURNING.", score: 0.701, chunk_idx: 28, rerankerScore: 0.79 },
      { id: "doc-5b9e3c8a#1", collection: "code-symbols", name: "scheduler.py:assign_worker", snippet: "def assign_worker(self, session: Session) -> str | None:\n    # Inverse of claim — used by the scheduler loop", score: 0.682, chunk_idx: 1, rerankerScore: 0.74 },
    ],
    agents: [
      { id: "agent#pr-reviewer", collection: "agents", name: "pr-reviewer", snippet: "**Reviews** pull requests and posts inline comments. Uses github-mcp toolset.", score: 0.681, chunk_idx: 0, rerankerScore: 0.72 },
      { id: "agent#code-explainer", collection: "agents", name: "code-explainer", snippet: "Walks junior engineers through unfamiliar **code** paths.", score: 0.612, chunk_idx: 0, rerankerScore: 0.65 },
    ],
    graphs: [
      { id: "graph#graph-tier1-escalation", collection: "graphs", name: "graph-tier1-escalation", snippet: "Tier-1 support triage → tier-2 escalation flow.", score: 0.502, chunk_idx: 0, rerankerScore: 0.55 },
    ],
    tools: [
      { id: "tool#fs.grep", collection: "_workspaces", name: "fs.grep", snippet: "Search files with a pattern", score: 0.612, chunk_idx: 0, rerankerScore: 0.7 },
      { id: "tool#fs.read", collection: "_workspaces", name: "fs.read", snippet: "Read a file from the workspace", score: 0.582, chunk_idx: 0, rerankerScore: 0.66 },
    ],
  };
  return (corpus[target] || corpus.collections).slice(0, topK);
}

window.CollectionsPage = CollectionsPage;
window.DocumentsPage = DocumentsPage;
window.SearchBench = SearchBench;
window.COLLECTIONS_INDEX = COLLECTIONS;
