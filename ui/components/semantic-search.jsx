/* global React, Icon, Btn, Modal, Banner, relativeTime, fmtDate */

function SSPListPage({ ssps, onOpen, onCreate, pushToast, ssmState }) {
  const [createOpen, setCreateOpen] = React.useState(false);

  if (ssmState === "none" || ssps.length === 0) {
    return (
      <>
        <Banner
          kind="warning"
          title="No Semantic Search providers configured"
          detail="Collections and the Internal Collections subsystem can't be created until you add one."
        />
        <div className="panel">
          <div className="empty">
            <div className="ico-wrap"><Icon name="subsystem" size={22} /></div>
            <div className="head">No semantic search providers</div>
            <div className="sub">
              A SemanticSearchProvider is the database (pgvector or pgvectorscale) that stores vector indexes for your collections.
            </div>
            <div className="actions">
              <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New Semantic Search provider</Btn>
              <Btn kind="ghost" icon="external">Read the docs</Btn>
            </div>
          </div>
        </div>
        {createOpen && (
          <SSPCreateModal
            onClose={() => setCreateOpen(false)}
            onCreate={(p) => { setCreateOpen(false); onCreate(p); }}
          />
        )}
      </>
    );
  }

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter providers…" />
        </div>
        <div className="sep-v" />
        <select className="select"><option>all backends</option><option>pgvector</option><option>pgvectorscale</option></select>
        <span className="muted text-sm tabular" style={{ marginLeft: "auto" }}>
          <span className="mono" style={{ color: "var(--green)" }}>● live</span> · /v1/ssp every 5s
        </span>
        <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New provider</Btn>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Backend</th>
              <th>Host</th>
              <th>Schema</th>
              <th>Status</th>
              <th>Last invalidated</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {ssps.map((p) => (
              <tr key={p.id} onClick={() => onOpen(p.id)}>
                <td className="mono">{p.id}</td>
                <td><BackendBadge kind={p.provider} /></td>
                <td className="mono muted text-sm">{p.config.hostname}<span style={{ color: "var(--text-4)" }}>:{p.config.port}</span></td>
                <td className="mono muted text-sm">{p.config.schema}</td>
                <td>
                  {p.status === "ok" && <span className="pill pill-ended"><span className="dot"></span>reachable</span>}
                  {p.status === "amber" && <span className="pill pill-paused"><span className="dot"></span>invalidated</span>}
                  {p.status === "err" && <span className="pill pill-failed"><span className="dot"></span>unreachable</span>}
                </td>
                <td className="mono muted">{relativeTime(p.last_invalidated_ago)}</td>
                <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <SSPCreateModal
          onClose={() => setCreateOpen(false)}
          onCreate={(p) => { setCreateOpen(false); onCreate(p); }}
        />
      )}
    </div>
  );
}

function BackendBadge({ kind }) {
  const color = kind === "pgvector" ? "var(--blue)" : "var(--violet)";
  return (
    <span className="pill" style={{ background: "var(--bg-2)", color, border: "1px solid var(--border)" }}>
      <span className="dot" style={{ background: color }}></span>
      <span className="mono text-sm">{kind}</span>
    </span>
  );
}

// ----------------------------------------------------------------------
// Create modal
// ----------------------------------------------------------------------

function SSPCreateModal({ onClose, onCreate }) {
  const [form, setForm] = React.useState({
    id: `pg-${Math.random().toString(36).slice(2, 8)}`,
    provider: "pgvector",
    hostname: "",
    port: 5432,
    database: "matrix",
    username: "",
    password: "",
    schema: "public",
    hnsw_m: 16,
    hnsw_ef_construction: 64,
    enable_diskann: false,
    num_neighbors: 50,
    search_list_size: 100,
  });
  const [errors, setErrors] = React.useState({});

  const update = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const isScale = form.provider === "pgvectorscale";

  const submit = () => {
    const errs = {};
    if (!form.hostname) errs.hostname = "value is required";
    if (!form.username) errs.username = "value is required";
    if (!form.password) errs.password = "value is required";
    if (!form.database) errs.database = "value is required";
    if (Object.keys(errs).length > 0) {
      setErrors(errs);
      return;
    }
    setErrors({});
    onCreate(form);
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal"
        style={{
          width: 540,
          maxWidth: "calc(100vw - 40px)",
          maxHeight: "calc(100vh - 40px)",
          display: "flex",
          flexDirection: "column",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-h">
          <span className="title">New Semantic Search provider</span>
          <button className="close" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>
        <div className="modal-b" style={{ overflow: "auto", flex: 1, minHeight: 0 }}>
          <FieldRow label="id" hint="must be unique" err={errors.id}>
            <input className="input mono" value={form.id} onChange={(e) => update("id", e.target.value)} style={{ width: "100%" }} />
          </FieldRow>
          <FieldRow label="backend">
            <select className="select mono" value={form.provider} onChange={(e) => update("provider", e.target.value)} style={{ width: "100%" }}>
              <option value="pgvector">pgvector</option>
              <option value="pgvectorscale">pgvectorscale</option>
            </select>
          </FieldRow>

          <Section label="Connection" />
          <FieldRow label="hostname" err={errors.hostname}>
            <input className="input mono" value={form.hostname} onChange={(e) => update("hostname", e.target.value)} placeholder="pg-prod.internal" style={{ width: "100%" }} />
          </FieldRow>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 100px", gap: 10 }}>
            <FieldRow label="database" err={errors.database}>
              <input className="input mono" value={form.database} onChange={(e) => update("database", e.target.value)} style={{ width: "100%" }} />
            </FieldRow>
            <FieldRow label="port">
              <input className="input mono" type="number" value={form.port} onChange={(e) => update("port", +e.target.value)} style={{ width: "100%" }} />
            </FieldRow>
          </div>
          <FieldRow label="username" err={errors.username}>
            <input className="input mono" value={form.username} onChange={(e) => update("username", e.target.value)} placeholder="matrix_rw" style={{ width: "100%" }} />
          </FieldRow>
          <FieldRow label="password" hint="SecretStr · stored encrypted" err={errors.password}>
            <input className="input mono" type="password" value={form.password} onChange={(e) => update("password", e.target.value)} placeholder="•••••••••" style={{ width: "100%" }} />
          </FieldRow>
          <FieldRow label="schema">
            <input className="input mono" value={form.schema} onChange={(e) => update("schema", e.target.value)} style={{ width: "100%" }} />
          </FieldRow>

          <Section label="HNSW knobs" />
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
            <FieldRow label="M" hint="graph degree">
              <input className="input mono" type="number" value={form.hnsw_m} onChange={(e) => update("hnsw_m", +e.target.value)} style={{ width: "100%" }} />
            </FieldRow>
            <FieldRow label="ef_construction" hint="build-time accuracy">
              <input className="input mono" type="number" value={form.hnsw_ef_construction} onChange={(e) => update("hnsw_ef_construction", +e.target.value)} style={{ width: "100%" }} />
            </FieldRow>
          </div>

          <Section label="DiskANN" sub="pgvectorscale only" />
          {!isScale && (
            <div className="banner banner-info" style={{ fontSize: 11.5, padding: "6px 10px", marginBottom: 10 }}>
              <Icon name="info" size={11} className="ico" />
              <div>Switch backend to <span className="mono">pgvectorscale</span> to enable DiskANN.</div>
            </div>
          )}
          <fieldset disabled={!isScale} style={{ border: 0, padding: 0, margin: 0, opacity: isScale ? 1 : 0.4 }}>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 8, marginBottom: 10, fontSize: 12.5 }}>
              <input type="checkbox" checked={form.enable_diskann} onChange={(e) => update("enable_diskann", e.target.checked)} />
              <span>enable DiskANN index</span>
            </label>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <FieldRow label="num_neighbors">
                <input className="input mono" type="number" value={form.num_neighbors} onChange={(e) => update("num_neighbors", +e.target.value)} disabled={!form.enable_diskann} style={{ width: "100%" }} />
              </FieldRow>
              <FieldRow label="search_list_size">
                <input className="input mono" type="number" value={form.search_list_size} onChange={(e) => update("search_list_size", +e.target.value)} disabled={!form.enable_diskann} style={{ width: "100%" }} />
              </FieldRow>
            </div>
          </fieldset>
        </div>
        <div className="modal-f">
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit}>Create</Btn>
        </div>
      </div>
    </div>
  );
}

function FieldRow({ label, hint, err, children }) {
  return (
    <div className="field">
      <label className="field-label">
        {label}
        {hint && <span className="hint">{hint}</span>}
      </label>
      {children}
      {err && <div className="field-help" style={{ color: "var(--red)" }}>
        <Icon name="x-circle" size={11} style={{ verticalAlign: -1, marginRight: 3 }} />
        {err}
      </div>}
    </div>
  );
}

function Section({ label, sub }) {
  return (
    <div style={{ borderBottom: "1px dashed var(--border)", marginBottom: 12, paddingBottom: 4, marginTop: 6 }}>
      <span className="mono" style={{ fontSize: 10.5, fontWeight: 600, color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
      {sub && <span className="muted text-sm" style={{ marginLeft: 8, fontSize: 11 }}>· {sub}</span>}
    </div>
  );
}

// ----------------------------------------------------------------------
// Detail page
// ----------------------------------------------------------------------

function SSPDetail({ sspId, ssps, onBack, pushToast, onDelete }) {
  const p = ssps.find((x) => x.id === sspId);
  const [tab, setTab] = React.useState("overview");
  const [showDelete, setShowDelete] = React.useState(false);

  if (!p) return null;

  const referencingCollections = (window.COLLECTIONS_INDEX || []).filter((c) => c.search_provider_id === sspId);

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div className="panel-body" style={{ padding: "14px 18px", display: "flex", alignItems: "center", gap: 14 }}>
          <BackendBadge kind={p.provider} />
          <div style={{ flex: 1 }}>
            <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{p.id}</div>
            <div className="muted text-sm mono">
              {p.config.username}@{p.config.hostname}:{p.config.port}/{p.config.database} · schema {p.config.schema}
            </div>
          </div>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={() => pushToast({ kind: "success", title: "Cache invalidated", detail: `POST /v1/ssp/${p.id}/invalidate → 200. Next call resolves a fresh connection.` })}>Invalidate</Btn>
          <Btn size="sm" kind="danger" icon="trash" onClick={() => setShowDelete(true)}>Delete</Btn>
        </div>

        <div style={{ display: "flex", alignItems: "center", borderTop: "1px solid var(--border)", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {[
            { id: "overview", label: "Overview", icon: "info" },
            { id: "config", label: "Config", icon: "settings" },
            { id: "collections", label: "Collections", icon: "collection", count: referencingCollections.length },
          ].map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                background: "none", border: "none",
                padding: "10px 14px", cursor: "pointer",
                color: tab === t.id ? "var(--text)" : "var(--text-3)",
                fontSize: 12.5, fontWeight: tab === t.id ? 600 : 400,
                borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
                display: "inline-flex", alignItems: "center", gap: 6,
              }}
            >
              <Icon name={t.icon} size={13} />
              {t.label}
              {t.count != null && t.count > 0 && <span className="count" style={{ marginLeft: 4 }}>{t.count}</span>}
            </button>
          ))}
        </div>

        <div style={{ padding: 18 }}>
          {tab === "overview" && <SSPOverview p={p} />}
          {tab === "config" && <SSPConfig p={p} />}
          {tab === "collections" && <SSPCollections collections={referencingCollections} sspId={sspId} />}
        </div>
      </div>

      {showDelete && (
        <Modal
          title={`Delete ${sspId}?`}
          danger
          onClose={() => setShowDelete(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setShowDelete(false)}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                disabled={referencingCollections.length > 0}
                onClick={() => { setShowDelete(false); onDelete(sspId); }}
              >
                Delete provider
              </Btn>
            </>
          }
        >
          {referencingCollections.length > 0 ? (
            <>
              <strong style={{ color: "var(--red)" }}>409 Conflict</strong> — this provider is referenced by{" "}
              <strong>{referencingCollections.length}</strong> collection{referencingCollections.length === 1 ? "" : "s"}:
              <ul>
                {referencingCollections.slice(0, 6).map((c) => <li key={c.id} className="mono">{c.id}</li>)}
              </ul>
              Reassign or delete those collections first.
            </>
          ) : (
            <>
              No collections reference this provider. Deletion is safe.
              <ul>
                <li>The vector tables in <span className="mono">{p.config.schema}</span> are <strong>not</strong> dropped.</li>
                <li>The provider row is removed; the cached connection is closed.</li>
              </ul>
            </>
          )}
        </Modal>
      )}
    </div>
  );
}

function SSPOverview({ p }) {
  return (
    <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
      <dt>id</dt><dd>{p.id}</dd>
      <dt>backend</dt><dd><BackendBadge kind={p.provider} /></dd>
      <dt>host</dt><dd>{p.config.hostname}:{p.config.port}</dd>
      <dt>database</dt><dd>{p.config.database}</dd>
      <dt>schema</dt><dd>{p.config.schema}</dd>
      <dt>created</dt><dd>{relativeTime(p.created_at_ago)}</dd>
      <dt>last_invalidated_at</dt><dd>{relativeTime(p.last_invalidated_ago)}</dd>
    </dl>
  );
}

function SSPConfig({ p }) {
  // Redacted config: password masked
  const redacted = {
    ...p.config,
    password: "**********",
  };
  return (
    <div>
      <div className="muted text-sm mb-2">Server returns this redacted config — the password is never sent over the wire.</div>
      <div className="code-block" style={{ maxHeight: 360, overflow: "auto" }}>
        {JSON.stringify({ id: p.id, provider: p.provider, config: redacted }, null, 2)}
      </div>
      <div className="mt-3">
        <Btn size="sm" kind="ghost" icon="copy">Edit</Btn>
      </div>
    </div>
  );
}

function SSPCollections({ collections, sspId }) {
  if (collections.length === 0) {
    return (
      <div className="empty" style={{ padding: 20 }}>
        <div className="ico-wrap"><Icon name="collection" size={18} /></div>
        <div className="head">No collections bound</div>
        <div className="sub">No collections are using this provider yet. Create one and select <span className="mono">{sspId}</span>.</div>
      </div>
    );
  }
  return (
    <table className="tbl">
      <thead><tr><th>ID</th><th>Description</th><th>Embedding</th><th style={{ textAlign: "right" }}>Docs</th><th>Activated</th></tr></thead>
      <tbody>
        {collections.map((c) => (
          <tr key={c.id}>
            <td className="mono">{c.id}</td>
            <td className="muted">{c.desc || <span style={{ color: "var(--text-4)" }}>—</span>}</td>
            <td className="mono muted text-sm">{c.embedding_provider} <span style={{ color: "var(--text-4)" }}>· {c.model}</span></td>
            <td className="mono num tabular">{c.docs?.toLocaleString() || 0}</td>
            <td className="mono muted">{relativeTime(c.last_ingest || 0)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

window.SSPListPage = SSPListPage;
window.SSPDetail = SSPDetail;
window.BackendBadge = BackendBadge;
