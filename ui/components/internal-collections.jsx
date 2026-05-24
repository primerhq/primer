/* global React, Icon, Btn, Banner, relativeTime */

function InternalCollectionsPage({ state, setState, pushToast, ssps, ssmState, onNavigate }) {
  if (state === "inactive") return <Inactive onConfigure={() => setState("configured")} ssps={ssps} ssmState={ssmState} onNavigate={onNavigate} />;
  if (state === "configured") return <Configured onBootstrap={() => { setState("active"); pushToast({ kind: "success", title: "Bootstrap complete", detail: "Ingested 8 agents, 2 graphs, 4 collections, and 8 tools." }); }} onCancel={() => setState("inactive")} ssps={ssps} />;
  return <Active onReboot={() => pushToast({ kind: "info", title: "Re-bootstrapping…", detail: "Re-running ingestion across all sources." })} onDeactivate={() => setState("inactive")} ssps={ssps} />;
}

// ----- State 1: Inactive
function Inactive({ onConfigure, ssps, ssmState, onNavigate }) {
  const [showForm, setShowForm] = React.useState(false);
  const [sspId, setSspId] = React.useState(ssps[0]?.id || "");
  const noSSPs = !ssps || ssps.length === 0;
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div className="panel-body" style={{ padding: 32, textAlign: "center" }}>
          <div style={{ width: 72, height: 72, margin: "0 auto 14px", borderRadius: 16, background: "var(--bg-2)", display: "grid", placeItems: "center" }}>
            <Icon name="subsystem" size={32} className="muted" />
          </div>
          <h2 style={{ margin: "0 0 6px", fontSize: 18, fontWeight: 600 }}>Internal Collections is inactive</h2>
          <div className="muted" style={{ maxWidth: 520, margin: "0 auto" }}>
            Activate to enable semantic search across agents, graphs, collections, and tools. While inactive, all <span className="mono" style={{ color: "var(--text)" }}>/search</span> routes return 503.
          </div>
          <div style={{ marginTop: 16, display: "flex", justifyContent: "center", gap: 8 }}>
            <Btn kind="primary" icon="settings" onClick={() => setShowForm(true)}>Configure</Btn>
            <Btn kind="ghost" icon="external">Read the docs</Btn>
          </div>
        </div>
      </div>

      {showForm && (
        <div className="panel">
          <div className="panel-h">
            <Icon name="settings" size={13} className="muted" />
            <span>Configuration</span>
          </div>
          <div className="panel-body">
            {noSSPs && (
              <Banner
                kind="warning"
                title="No Semantic Search providers configured"
                detail="The internal collections subsystem needs an SSP to back its four reserved collections."
                actions={<Btn size="sm" kind="primary" icon="plus" onClick={() => onNavigate("semantic-search")}>Create one</Btn>}
              />
            )}
            <div className="field">
              <label className="field-label">Semantic Search provider <span className="hint">required — backs the 4 reserved internal collections</span></label>
              <select className="select mono" disabled={noSSPs} value={sspId} onChange={(e) => setSspId(e.target.value)} style={{ width: "100%" }}>
                {noSSPs && <option value="">— no providers configured —</option>}
                {ssps.map((p) => <option key={p.id} value={p.id}>{p.id} · {p.provider}</option>)}
              </select>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
              <div className="field">
                <label className="field-label">Embedding provider</label>
                <select className="select mono" style={{ width: "100%" }}>
                  <option>openai-emb-1</option>
                  <option>voyage-1</option>
                </select>
              </div>
              <div className="field">
                <label className="field-label">Embedding model</label>
                <select className="select mono" style={{ width: "100%" }}>
                  <option>text-embedding-3-large</option>
                  <option>text-embedding-3-small</option>
                </select>
              </div>
              <div className="field">
                <label className="field-label">Cross-encoder rerank <span className="hint">optional</span></label>
                <select className="select mono" style={{ width: "100%" }}>
                  <option>none</option>
                  <option>cohere-rerank-1 / rerank-english-v3</option>
                </select>
              </div>
              <div className="field">
                <label className="field-label">MMR config <span className="hint">optional</span></label>
                <input className="input mono" placeholder='{"lambda": 0.5, "k": 25}' />
              </div>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 8 }}>
              <Btn kind="ghost" onClick={() => setShowForm(false)}>Cancel</Btn>
              <Btn kind="primary" icon="check" disabled={noSSPs || !sspId} onClick={onConfigure}>Save → ready to bootstrap</Btn>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ----- State 2: Configured but not bootstrapped
function Configured({ onBootstrap, onCancel, ssps }) {
  const [running, setRunning] = React.useState(false);
  const sspId = (ssps && ssps[0] && ssps[0].id) || "(unset)";
  const [progress, setProgress] = React.useState({ agents: 0, graphs: 0, collections: 0, tools: 0 });

  const start = () => {
    setRunning(true);
    const steps = [
      { agents: 3 }, { agents: 6 }, { agents: 8, graphs: 1 }, { graphs: 2 },
      { collections: 2 }, { collections: 4, tools: 4 }, { tools: 14 }, { tools: 28 },
    ];
    let i = 0;
    const id = setInterval(() => {
      if (i >= steps.length) {
        clearInterval(id);
        setRunning(false);
        onBootstrap();
        return;
      }
      setProgress((p) => ({ ...p, ...steps[i] }));
      i += 1;
    }, 480);
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <Banner
        kind="info"
        title="Subsystem configured"
        detail="Bootstrap is required before /search routes return results. First bootstrap ingests existing entities and can take 30–60s."
      />

      <div className="panel">
        <div className="panel-h">
          <Icon name="settings" size={13} className="muted" />
          <span>Configuration</span>
          <div className="right">
            <Btn size="sm" kind="ghost" icon="settings">Update config</Btn>
          </div>
        </div>
        <div className="panel-body">
          <div className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
            <dt>search_provider_id</dt><dd className="mono">{sspId}</dd>
            <dt>embedding_provider_id</dt><dd>openai-emb-1</dd>
            <dt>embedding_model</dt><dd>text-embedding-3-large</dd>
            <dt>reranker</dt><dd className="muted">(none)</dd>
            <dt>mmr</dt><dd className="muted">disabled</dd>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-h">
          <Icon name="play" size={13} className="muted" />
          <span>Bootstrap</span>
          {running && <span className="sub">· running…</span>}
        </div>
        <div className="panel-body">
          <div className="muted text-sm mb-3">
            Bootstrap ingests every existing agent, graph, collection, and tool into the Internal Collections vector store. Idempotent — safe to re-run.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10, marginBottom: 14 }}>
            <ProgressStat label="agents" value={progress.agents} target={8} running={running} />
            <ProgressStat label="graphs" value={progress.graphs} target={2} running={running} />
            <ProgressStat label="collections" value={progress.collections} target={4} running={running} />
            <ProgressStat label="tools" value={progress.tools} target={28} running={running} />
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            {!running ? (
              <>
                <Btn kind="primary" icon="play" onClick={start}>Bootstrap now</Btn>
                <Btn kind="ghost" onClick={onCancel}>Deactivate</Btn>
              </>
            ) : (
              <Btn kind="ghost" disabled>
                <Icon name="zap" size={11} style={{ color: "var(--accent)", animation: "spin 1s linear infinite" }} />
                Bootstrapping…
              </Btn>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function ProgressStat({ label, value, target, running }) {
  const pct = (value / target) * 100;
  return (
    <div className="panel">
      <div className="panel-body" style={{ padding: "10px 14px" }}>
        <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>{label}</div>
        <div className="mono tabular" style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>
          {value}<span className="muted text-sm">/{target}</span>
        </div>
        <div style={{ height: 4, background: "var(--bg-2)", borderRadius: 2, overflow: "hidden", marginTop: 6 }}>
          <div style={{ width: `${pct}%`, height: "100%", background: running ? "var(--accent)" : "var(--accent-2)", transition: "width 0.3s" }}></div>
        </div>
      </div>
    </div>
  );
}

// ----- State 3: Active
function Active({ onReboot, onDeactivate, ssps }) {
  const sspId = (ssps && ssps[0] && ssps[0].id) || "(unset)";
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel" style={{
        background: "linear-gradient(90deg, var(--green-dim) 0%, var(--bg-1) 60%)",
        borderColor: "oklch(0.75 0.15 145 / 0.3)",
      }}>
        <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 16, padding: "18px 22px" }}>
          <div style={{ width: 48, height: 48, borderRadius: 10, background: "var(--green)", display: "grid", placeItems: "center", boxShadow: "0 0 0 4px var(--green-dim)" }}>
            <Icon name="check" size={22} style={{ color: "var(--accent-fg)" }} />
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600 }}>Internal Collections is active</div>
            <div className="muted text-sm">Last bootstrap 14 minutes ago · CDC worker syncing every 5s</div>
          </div>
          <Btn icon="search">Run a search</Btn>
        </div>
      </div>

      <Banner
        kind="info"
        title="Eventual consistency"
        detail="The CDC worker syncs new entities asynchronously (within a bounded window). Newly-created agents, graphs, collections, and tools may take a few seconds to appear in search results."
      />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10 }}>
        <ActiveStat label="agents" docs={8} chunks={42} />
        <ActiveStat label="graphs" docs={2} chunks={6} />
        <ActiveStat label="collections" docs={4} chunks={12} />
        <ActiveStat label="tools" docs={28} chunks={28} />
      </div>

      <div className="panel">
        <div className="panel-h">
          <Icon name="settings" size={13} className="muted" />
          <span>Configuration</span>
          <div className="right">
            <Btn size="sm" kind="ghost" icon="settings">Update config</Btn>
          </div>
        </div>
        <div className="panel-body">
          <div className="kv" style={{ gridTemplateColumns: "220px 1fr" }}>
            <dt>search_provider_id</dt><dd className="mono">{sspId}</dd>
            <dt>embedding_provider_id</dt><dd>openai-emb-1</dd>
            <dt>embedding_model</dt><dd>text-embedding-3-large</dd>
            <dt>reranker</dt><dd className="muted">(none)</dd>
            <dt>mmr</dt><dd className="muted">disabled</dd>
            <dt>last_bootstrap_at</dt><dd>14m ago</dd>
            <dt>cdc_lag</dt><dd className="mono" style={{ color: "var(--green)" }}>0.4s</dd>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-h">
          <Icon name="play" size={13} className="muted" />
          <span>Actions</span>
        </div>
        <div className="panel-body" style={{ display: "flex", gap: 8 }}>
          <Btn icon="refresh" onClick={onReboot}>Re-bootstrap</Btn>
          <Btn icon="settings" kind="ghost">Update config</Btn>
          <Btn icon="trash" kind="danger" onClick={onDeactivate} style={{ marginLeft: "auto" }}>Deactivate</Btn>
        </div>
      </div>
    </div>
  );
}

function ActiveStat({ label, docs, chunks }) {
  return (
    <div className="panel">
      <div className="panel-body" style={{ padding: "12px 14px" }}>
        <div className="muted text-sm mono" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontSize: 10.5 }}>{label}</div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 6, marginTop: 4 }}>
          <span className="mono tabular" style={{ fontSize: 20, fontWeight: 600 }}>{docs}</span>
          <span className="muted text-sm">docs</span>
        </div>
        <div className="muted text-sm mono">{chunks} chunks</div>
      </div>
    </div>
  );
}

window.InternalCollectionsPage = InternalCollectionsPage;
