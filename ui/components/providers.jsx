/* global React, Icon, Btn, Banner, relativeTime */

const PROVIDERS = {
  llm: [
    { id: "openai-1", vendor: "openai", api_base: "https://api.openai.com/v1", api_key_set: true, models: [{ id: "gpt-4o", ctx: 128000, tags: ["fast"] }, { id: "gpt-4o-mini", ctx: 128000, tags: ["cheap", "fast"] }, { id: "o1", ctx: 200000, tags: ["reasoning"] }], invalidate_ago: 180 },
    { id: "anthropic-1", vendor: "anthropic", api_base: "https://api.anthropic.com/v1", api_key_set: true, models: [{ id: "claude-sonnet-4", ctx: 200000, tags: ["balanced"] }, { id: "claude-haiku-4-5", ctx: 200000, tags: ["fast"] }, { id: "claude-opus-4", ctx: 200000, tags: ["reasoning"] }], invalidate_ago: 60 * 30 },
    { id: "openai-deleted", vendor: "openai", api_base: "https://api.openai.com/v1", api_key_set: false, models: [], invalidate_ago: 3600 * 12, broken: true },
  ],
  embedding: [
    { id: "openai-emb-1", vendor: "openai", api_base: "https://api.openai.com/v1", api_key_set: true, models: [{ id: "text-embedding-3-large", ctx: 8192, tags: ["3072d"] }, { id: "text-embedding-3-small", ctx: 8192, tags: ["1536d"] }], invalidate_ago: 60 },
    { id: "voyage-1", vendor: "voyageai", api_base: "https://api.voyageai.com/v1", api_key_set: true, models: [{ id: "voyage-3", ctx: 32000, tags: ["1024d"] }, { id: "voyage-3-lite", ctx: 32000, tags: ["512d"] }], invalidate_ago: 3600 },
  ],
  rerank: [
    { id: "cohere-rerank-1", vendor: "cohere", api_base: "https://api.cohere.com/v1", api_key_set: true, models: [{ id: "rerank-english-v3", ctx: 4096, tags: ["english"] }, { id: "rerank-multilingual-v3", ctx: 4096, tags: ["multilingual"] }], invalidate_ago: 3600 * 4 },
  ],
};

const VENDOR_COLORS = {
  openai: "var(--green)",
  anthropic: "var(--accent)",
  voyageai: "var(--blue)",
  cohere: "var(--violet)",
};

function ProvidersPage({ kind, sessions, pushToast }) {
  const items = PROVIDERS[kind] || [];
  const [sel, setSel] = React.useState(items[0]?.id || null);
  const selected = items.find((p) => p.id === sel);

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter providers…" />
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus">New provider</Btn>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "320px 1fr", gap: 18 }}>
        <div className="col" style={{ gap: 6 }}>
          {items.map((p) => (
            <div
              key={p.id}
              onClick={() => setSel(p.id)}
              className="panel"
              style={{
                cursor: "pointer",
                borderColor: sel === p.id ? "var(--accent)" : "var(--border)",
                background: sel === p.id ? "var(--accent-dim)" : undefined,
              }}
            >
              <div className="panel-body" style={{ padding: "10px 12px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{ width: 8, height: 8, borderRadius: "50%", background: VENDOR_COLORS[p.vendor] || "var(--text-3)" }}></div>
                  <span className="mono" style={{ fontWeight: 500 }}>{p.id}</span>
                  <div style={{ marginLeft: "auto" }}>
                    {p.broken ? <span className="pill pill-failed"><span className="dot"></span>broken</span> : <span className="pill pill-ended"><span className="dot"></span>ok</span>}
                  </div>
                </div>
                <div className="muted text-sm mt-2" style={{ fontSize: 11 }}>
                  <span className="mono">{p.vendor}</span> · {p.models.length} model{p.models.length === 1 ? "" : "s"} · invalidated {relativeTime(p.invalidate_ago)}
                </div>
              </div>
            </div>
          ))}
        </div>

        {selected && <ProviderDetail kind={kind} p={selected} sessions={sessions} pushToast={pushToast} />}
      </div>
    </div>
  );
}

function ProviderDetail({ kind, p, sessions, pushToast }) {
  const [showKey, setShowKey] = React.useState(false);
  const usedIn = window.MOCK.AGENTS.filter((a) => {
    const d = (window.AGENT_DETAILS_FOR_PROVIDER && window.AGENT_DETAILS_FOR_PROVIDER[a.id]) || {};
    return d.llm_provider_id === p.id;
  });

  // Just use the simpler check
  const referencingAgents = kind === "llm" ? lookupAgentsByProvider(p.id) : [];
  const referencingCollections = kind === "embedding" ? lookupCollectionsByEmbProvider(p.id) : [];

  return (
    <div className="col" style={{ gap: 14 }}>
      {p.broken && (
        <Banner
          kind="error"
          title="Provider broken"
          detail="No API key set — every call returns 502 /errors/provider-server-error."
          actions={<Btn size="sm" kind="primary" icon="key">Set API key</Btn>}
        />
      )}

      <div className="panel">
        <div className="panel-h">
          <div style={{ width: 8, height: 8, borderRadius: "50%", background: VENDOR_COLORS[p.vendor] || "var(--text-3)" }}></div>
          <span className="mono" style={{ fontSize: 14, fontWeight: 600 }}>{p.id}</span>
          <div className="right">
            <Btn size="sm" kind="ghost" icon="refresh" onClick={() => pushToast({ kind: "success", title: "Cache invalidated", detail: `POST /v1/${kind}_providers/${p.id}/invalidate → 200` })}>Invalidate</Btn>
            <Btn size="sm" kind="ghost" icon="external">View JSON</Btn>
          </div>
        </div>
        <div className="panel-body">
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18 }}>
            <div className="col">
              <div className="field">
                <label className="field-label">id <span className="hint">read-only</span></label>
                <input className="input mono" value={p.id} readOnly />
              </div>
              <div className="field">
                <label className="field-label">vendor</label>
                <select className="select mono" defaultValue={p.vendor} style={{ width: "100%" }}>
                  <option>openai</option><option>anthropic</option><option>voyageai</option><option>cohere</option>
                </select>
              </div>
              <div className="field">
                <label className="field-label">api_base</label>
                <input className="input mono" defaultValue={p.api_base} />
              </div>
              <div className="field">
                <label className="field-label">api_key <span className="hint">SecretStr</span></label>
                <div style={{ display: "flex", gap: 6 }}>
                  <input
                    className="input mono"
                    type={showKey ? "text" : "password"}
                    value={p.api_key_set ? (showKey ? "sk-proj-abc123…xyz" : "•••••••• (set)") : ""}
                    readOnly
                    style={{ flex: 1 }}
                    placeholder="not set"
                  />
                  <button className="icon-btn" onClick={() => setShowKey(!showKey)} title={showKey ? "Hide" : "Show"}><Icon name={showKey ? "x" : "key"} size={12} /></button>
                  <Btn size="sm" kind="ghost">Replace</Btn>
                </div>
                <div className="field-help">Replace must be clicked explicitly — avoids accidental clears.</div>
              </div>
            </div>

            <div className="col">
              <div className="muted text-sm mono mb-2" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>used in</div>
              {referencingAgents.length === 0 && referencingCollections.length === 0 ? (
                <div className="muted text-sm">No references yet.</div>
              ) : (
                <>
                  {referencingAgents.map((a) => (
                    <div key={a} className="ref-row">
                      <Icon name="agent" size={13} className="ico" />
                      <span className="label">Agent</span>
                      <span className="val"><a>{a}</a></span>
                    </div>
                  ))}
                  {referencingCollections.map((c) => (
                    <div key={c} className="ref-row">
                      <Icon name="collection" size={13} className="ico" />
                      <span className="label">Collection</span>
                      <span className="val"><a>{c}</a></span>
                    </div>
                  ))}
                </>
              )}
              <div className="muted text-sm mt-3 mb-2 mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>last invalidate</div>
              <div className="mono">{relativeTime(p.invalidate_ago)}</div>
            </div>
          </div>
        </div>
      </div>

      {/* Models */}
      <div className="panel">
        <div className="panel-h">
          <Icon name={kind === "embedding" ? "emb" : kind === "rerank" ? "emb" : "llm"} size={13} className="muted" />
          <span>Models</span>
          <span className="sub">· static list from provider row</span>
          <div className="right">
            <Btn size="sm" kind="ghost" icon="refresh">Refresh from provider</Btn>
          </div>
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {p.models.length === 0 ? (
            <div className="empty" style={{ padding: 20 }}>
              <div className="muted text-sm">No models configured.</div>
            </div>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th>model</th>
                  <th>tags</th>
                  <th style={{ textAlign: "right" }}>context</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {p.models.map((m) => (
                  <tr key={m.id}>
                    <td className="mono">{m.id}</td>
                    <td>
                      <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                        {m.tags.map((t) => (
                          <span key={t} className="pill" style={{ background: "var(--bg-2)", color: "var(--text-2)", border: "1px solid var(--border)", fontSize: 10 }}>{t}</span>
                        ))}
                      </div>
                    </td>
                    <td className="mono num tabular muted">{m.ctx.toLocaleString()}</td>
                    <td style={{ textAlign: "right", paddingRight: 12 }}>
                      <button className="icon-btn" style={{ width: 22, height: 22 }} title="Delete"><Icon name="x" size={10} /></button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div style={{ padding: 10, borderTop: "1px solid var(--border)" }}>
            <Btn size="sm" kind="ghost" icon="plus">Add model</Btn>
            <span className="muted text-sm" style={{ marginLeft: 10 }}>
              <Icon name="info" size={11} style={{ verticalAlign: -2 }} /> "Refresh" returns the row's static list, not a live introspection of the upstream API.
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function lookupAgentsByProvider(providerId) {
  // This is a hack since AGENT_DETAILS is local to agents.jsx
  return Object.entries(window.AGENT_DETAILS_INDEX || {})
    .filter(([_, d]) => d.llm_provider_id === providerId)
    .map(([id]) => id);
}

function lookupCollectionsByEmbProvider(providerId) {
  return (window.COLLECTIONS_INDEX || []).filter((c) => c.embedding_provider === providerId).map((c) => c.id);
}

window.ProvidersPage = ProvidersPage;
