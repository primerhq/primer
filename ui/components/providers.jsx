/* global React, Icon, Btn, Modal, Banner */

const { apiFetch, useResource, useMutation, useRouter, useToast } = window.matrixApi;

const VENDOR_COLORS = {
  openai: "var(--green)",
  anthropic: "var(--accent)",
  voyageai: "var(--blue)",
  cohere: "var(--violet)",
  ollama: "var(--amber)",
  google: "var(--blue)",
  gemini: "var(--blue)",
  huggingface: "var(--amber)",
  openresponses: "var(--green)",
};

// kind URL segment + display label
const KINDS = {
  llm: { plural: "llm_providers", label: "LLM" },
  embedding: { plural: "embedding_providers", label: "Embedding" },
  rerank: { plural: "cross_encoder_providers", label: "Cross-Encoder" },
  cross_encoder: { plural: "cross_encoder_providers", label: "Cross-Encoder" },
};

// ============================================================================
// List page (parameterised by kind)
// ============================================================================

function ProvidersPage({ kind: kindProp }) {
  const { navigate } = useRouter();
  // Allow the kind to come either from a prop (legacy switch arm) or
  // from the route itself if mounted directly.
  const kindKey = kindProp === "rerank" ? "rerank" : kindProp;
  const k = KINDS[kindKey];
  if (!k) return <Banner kind="error" title="Unknown provider kind" detail={String(kindKey)} />;

  const { push: pushToast } = useToast();
  const list = useResource(`providers:${k.plural}`,
    (s) => apiFetch("GET", "/" + k.plural + "?limit=200", null, { signal: s }), {});
  const [createOpen, setCreateOpen] = React.useState(false);
  const [textFilter, setTextFilter] = React.useState("");

  const items = list.data?.items ?? [];
  const filtered = items.filter((p) => !textFilter || p.id.toLowerCase().includes(textFilter.toLowerCase()));

  return (
    <div className="col" style={{ gap: 14 }}>
      <ProvidersHeader label={k.label} plural={k.plural} count={items.length} onRefresh={list.refetch} onNew={() => setCreateOpen(true)} />

      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter providers…" value={textFilter} onChange={(e) => setTextFilter(e.target.value)} />
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New {k.label.toLowerCase()} provider</Btn>
        </div>
      </div>

      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Vendor</th>
              <th style={{ textAlign: "right" }}>Models</th>
            </tr>
          </thead>
          <tbody>
            {list.loading && items.length === 0 ? (
              <tr><td colSpan={3} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</td></tr>
            ) : list.error && items.length === 0 ? (
              <tr><td colSpan={3} style={{ padding: 20, textAlign: "center" }}>
                <span style={{ color: "var(--red)" }}>{list.error.title || list.error.message}</span>
                {" · "}<a onClick={list.refetch} style={{ cursor: "pointer" }}>Retry</a>
              </td></tr>
            ) : filtered.length === 0 ? (
              items.length === 0 ? (
                <tr><td colSpan={3}>
                  <div className="empty" style={{ padding: "40px 20px" }}>
                    <div className="ico-wrap"><Icon name={k.plural === "llm_providers" ? "llm" : "emb"} size={22} /></div>
                    <div className="head">No {k.label.toLowerCase()} providers yet</div>
                    <div className="sub">Providers wrap upstream APIs (OpenAI, Anthropic, etc.) and present a uniform shape to the rest of the system.</div>
                    <div className="actions"><Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New provider</Btn></div>
                  </div>
                </td></tr>
              ) : (
                <tr><td colSpan={3} className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No providers match.</td></tr>
              )
            ) : filtered.map((p) => {
              const color = VENDOR_COLORS[p.provider] || "var(--text-3)";
              const modelCount = (p.models || []).length;
              return (
                <tr key={p.id} onClick={() => navigate("/providers/" + kindKey + "/" + p.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{p.id}</td>
                  <td className="mono">
                    <span className="dot" style={{ background: color, marginRight: 6, display: "inline-block", width: 8, height: 8, borderRadius: "50%" }}></span>
                    {p.provider || "—"}
                  </td>
                  <td className="mono num tabular">{modelCount}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <NewProviderModal
          kindKey={kindKey}
          plural={k.plural}
          label={k.label}
          onClose={() => setCreateOpen(false)}
          onCreate={(p) => {
            setCreateOpen(false);
            pushToast({ kind: "success", title: "Provider created", detail: p.id });
            list.refetch();
            navigate("/providers/" + kindKey + "/" + p.id);
          }}
        />
      )}
    </div>
  );
}

function ProvidersHeader({ label, plural, count, onRefresh, onNew }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span>Providers</span><span className="sep">/</span><span style={{ color: "var(--text)" }}>{label}</span>
        </div>
        <h1 className="page-title">{label} providers</h1>
        <div className="page-sub tabular">{count} provider{count === 1 ? "" : "s"} · backed by <span className="mono">/v1/{plural}</span></div>
      </div>
      <div className="page-actions">
        <Btn icon="refresh" kind="ghost" onClick={onRefresh}>Refresh</Btn>
        <Btn icon="plus" kind="primary" onClick={onNew}>New {label.toLowerCase()} provider</Btn>
      </div>
    </div>
  );
}

// ============================================================================
// Create modal — generic JSON-config editor (per spec §3)
// ============================================================================

function NewProviderModal({ kindKey, plural, label, onClose, onCreate }) {
  const { push: pushToast } = useToast();
  const [id, setId] = React.useState("");
  const [provider, setProvider] = React.useState("");
  const [configJson, setConfigJson] = React.useState(_defaultConfigJson(kindKey));
  const [modelsJson, setModelsJson] = React.useState('[{"name": "model-name", "context_length": 4096}]');
  const [limitsJson, setLimitsJson] = React.useState('{"max_concurrency": 1}');
  const [fieldErrors, setFieldErrors] = React.useState({});

  const create = useMutation(
    (body) => apiFetch("POST", "/" + plural, body),
    {
      invalidates: [`providers:${plural}`],
      onSuccess: (p) => onCreate(p),
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
    let config, models, limits;
    try { config = JSON.parse(configJson); } catch (e) { setFieldErrors({ "body.config": "Invalid JSON: " + e.message }); return; }
    try { models = JSON.parse(modelsJson); } catch (e) { setFieldErrors({ "body.models": "Invalid JSON: " + e.message }); return; }
    try { limits = JSON.parse(limitsJson); } catch (e) { setFieldErrors({ "body.limits": "Invalid JSON: " + e.message }); return; }
    const body = {
      ...(id ? { id } : {}),
      provider,
      config,
      models,
      limits,
    };
    try { await create.mutate(body); } catch (_e) {}
  };

  return (
    <Modal
      title={`New ${label.toLowerCase()} provider`}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit} disabled={!provider || create.loading}>
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
        <label className="field-label">Vendor</label>
        <input className="input" value={provider} onChange={(e) => setProvider(e.target.value)} placeholder="openresponses, anthropic, openai, gemini, ollama, huggingface, voyageai, cohere, …" style={{ width: "100%" }} />
        {fieldErrors["body.provider"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.provider"]}</div>}
        <div className="field-help">
          Provider ↔ config alignment is NOT cross-validated server-side (T0379) — make sure
          the vendor name matches the config shape.
        </div>
      </div>
      <div className="field">
        <label className="field-label">Config (JSON)</label>
        <textarea className="textarea mono" value={configJson} onChange={(e) => setConfigJson(e.target.value)} rows={6} />
        {fieldErrors["body.config"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.config"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Models (JSON array)</label>
        <textarea className="textarea mono" value={modelsJson} onChange={(e) => setModelsJson(e.target.value)} rows={4} />
        {fieldErrors["body.models"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.models"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Limits (JSON)</label>
        <textarea className="textarea mono" value={limitsJson} onChange={(e) => setLimitsJson(e.target.value)} rows={2} />
        {fieldErrors["body.limits"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.limits"]}</div>}
      </div>
    </Modal>
  );
}

function _defaultConfigJson(kindKey) {
  if (kindKey === "embedding") return '{\n  "url": "https://api.openai.com/v1",\n  "api_key": "sk-...",\n  "flavor": "default"\n}';
  if (kindKey === "rerank") return '{\n  "url": "https://api.cohere.com/v1",\n  "api_key": "..."\n}';
  return '{\n  "url": "https://api.openai.com/v1",\n  "api_key": "sk-...",\n  "flavor": "other"\n}';
}

// ============================================================================
// Provider detail
// ============================================================================

function ProviderDetail({ kind: kindProp }) {
  const { params, navigate } = useRouter();
  const { push: pushToast } = useToast();
  const id = params.id;
  const kindKey = kindProp === "rerank" ? "rerank" : kindProp;
  const k = KINDS[kindKey];
  if (!k) return <Banner kind="error" title="Unknown provider kind" detail={String(kindKey)} />;

  const detail = useResource(`provider:${k.plural}:${id}`,
    (s) => apiFetch("GET", "/" + k.plural + "/" + encodeURIComponent(id), null, { signal: s }),
    { pollMs: null, deps: [k.plural, id] });
  const models = useResource(`provider-models:${k.plural}:${id}`,
    (s) => apiFetch("GET", "/" + k.plural + "/" + encodeURIComponent(id) + "/models", null, { signal: s }),
    { pollMs: null, deps: [k.plural, id] });

  const invalidate = useMutation(
    () => apiFetch("POST", "/" + k.plural + "/" + encodeURIComponent(id) + "/invalidate"),
    {
      invalidates: [`providers:${k.plural}`, `provider:${k.plural}:${id}`],
      onSuccess: () => pushToast({ kind: "info", title: "Cache dropped", detail: id }),
      onError: (err) => pushToast({ kind: "error", title: "Invalidate failed", detail: err.detail || err.message, requestId: err.requestId }),
    }
  );
  const delMut = useMutation(
    () => apiFetch("DELETE", "/" + k.plural + "/" + encodeURIComponent(id)),
    {
      invalidates: [`providers:${k.plural}`],
      onSuccess: () => { pushToast({ kind: "warning", title: "Provider deleted", detail: id }); navigate("/providers/" + kindKey); },
      onError: (err) => pushToast({ kind: "error", title: "Delete failed", detail: err.detail || err.message, requestId: err.requestId }),
    }
  );
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  if (detail.loading && !detail.data) {
    return <>
      <ProviderDetailHeader label={k.label} kindKey={kindKey} id={id} navigate={navigate} />
      <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
    </>;
  }
  if (detail.error && !detail.data) {
    return <>
      <ProviderDetailHeader label={k.label} kindKey={kindKey} id={id} navigate={navigate} />
      <Banner kind="error" title={detail.error.title || "Couldn't load provider"} detail={detail.error.detail || detail.error.message}
        actions={<Btn size="sm" icon="chevron-left" onClick={() => navigate("/providers/" + kindKey)}>Back to list</Btn>} />
    </>;
  }
  const p = detail.data;
  const color = VENDOR_COLORS[p.provider] || "var(--text-3)";
  const modelList = models.data?.models ?? [];

  return (
    <div className="col" style={{ gap: 14 }}>
      <ProviderDetailHeader
        label={k.label}
        kindKey={kindKey}
        id={id}
        navigate={navigate}
        onInvalidate={() => invalidate.mutate()}
        onDelete={() => setConfirmDelete(true)}
      />

      <div className="panel">
        <div className="panel-h">
          <span className="dot" style={{ background: color, display: "inline-block", width: 8, height: 8, borderRadius: "50%" }}></span>
          <span className="mono">{p.id}</span>
          <span className="sub mono">· {p.provider}</span>
        </div>
        <div className="panel-body">
          <div className="muted text-sm mb-3">
            Read-only render of the provider row. Edit via DELETE + POST; in-place PUT not exposed.
          </div>
          <div className="code-block" dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(_redactSecrets(p), null, 2)) }} />
        </div>
      </div>

      <div className="panel">
        <div className="panel-h">
          <Icon name="llm" size={13} className="muted" />
          <span>Models</span>
          <span className="sub">· returns the static row list, not a live introspection (T0025)</span>
          <div className="right">
            <Btn size="sm" icon="refresh" kind="ghost" onClick={models.refetch} disabled={models.loading}>Refresh</Btn>
          </div>
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {models.loading && modelList.length === 0 ? (
            <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
          ) : models.error ? (
            <div style={{ padding: 14 }}>
              <Banner kind="error" title={models.error.title || "Couldn't load models"} detail={models.error.detail || models.error.message} />
            </div>
          ) : modelList.length === 0 ? (
            <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>No models on this row.</div>
          ) : (
            <table className="tbl">
              <thead><tr><th>Model</th></tr></thead>
              <tbody>
                {modelList.map((m, i) => (
                  <tr key={typeof m === "string" ? m : (m.name || i)}>
                    <td className="mono">{typeof m === "string" ? m : m.name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="muted text-sm">
        "Used in" panel (agents / collections / IC config) is deferred —
        v1 doesn't reverse-index reference checks. Use the Agents page /
        Knowledge / Internal Collections wizard directly.
      </div>

      {confirmDelete && (
        <Modal
          title={`Delete ${id}?`}
          danger
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setConfirmDelete(false)}>Cancel</Btn>
              <Btn kind="danger" icon="trash" onClick={async () => { setConfirmDelete(false); try { await delMut.mutate(); } catch (_e) {} }}>Delete</Btn>
            </>
          }
        >
          <ul>
            <li>Removes the provider row from storage.</li>
            <li>Any agent / collection referencing this provider id will fail at next use.</li>
            <li>Invalidates the backend's cached adapter (DELETE is wired through <span className="mono">invalidate</span> on the workspace registry side).</li>
            <li>DELETE is NOT idempotent on entities — a second DELETE returns 404 (app spec §5).</li>
          </ul>
        </Modal>
      )}
    </div>
  );
}

function ProviderDetailHeader({ label, kindKey, id, navigate, onInvalidate, onDelete }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="crumb">
          <span>Providers</span><span className="sep">/</span>
          <a onClick={() => navigate("/providers/" + kindKey)}>{label}</a>
          <span className="sep">/</span>
          <span className="mono" style={{ color: "var(--text)" }}>{id}</span>
        </div>
        <h1 className="page-title mono">{id}</h1>
      </div>
      <div className="page-actions">
        {onInvalidate && <Btn icon="refresh" kind="ghost" onClick={onInvalidate}>Invalidate</Btn>}
        {onDelete && <Btn icon="trash" kind="danger" onClick={onDelete}>Delete</Btn>}
        <Btn icon="chevron-left" kind="ghost" onClick={() => navigate("/providers/" + kindKey)}>Back</Btn>
      </div>
    </div>
  );
}

function _redactSecrets(row) {
  // The backend already masks SecretStr fields on the wire (per app
  // spec §7), so this is defensive belt-and-braces in case a future
  // mistake echoes them. Scan for any string field named like
  // "api_key" / "password" / "secret" and stringify as "•••• (set)".
  const SECRET_RE = /(api_key|password|secret|token)/i;
  function walk(v) {
    if (v == null || typeof v !== "object") return v;
    if (Array.isArray(v)) return v.map(walk);
    const out = {};
    for (const [k, val] of Object.entries(v)) {
      if (typeof val === "string" && SECRET_RE.test(k) && val && !val.startsWith("•")) {
        out[k] = "•••• (redacted by UI)";
      } else {
        out[k] = walk(val);
      }
    }
    return out;
  }
  return walk(row);
}

window.ProvidersPage = ProvidersPage;
window.ProviderDetail = ProviderDetail;
