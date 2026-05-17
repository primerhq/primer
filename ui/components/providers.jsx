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
// Create modal — provider-pattern with rich per-provider controls.
// ============================================================================
//
// PROVIDER_FIELDS is the single source of truth mirroring the backend's
// provider enums + per-provider Config models in matrix/model/provider.py.
// Keep these in sync when the backend grows a new provider type.
//
// Each kind (llm / embedding / rerank) maps provider-type-string ->
//   { label, config: [field, ...], discoverable, suggestedModels, modelFields }
//
// field: { key, label, type, placeholder?, options?, required?, default? }
//   type: "text" | "password" | "url" | "enum"
//
// discoverable: when true, the "Fetch Models" button calls
//   POST /v1/{plural}/_discover_models with the draft {provider, config}
//   and replaces the models table with the response. When false, the
//   button populates the table from `suggestedModels` (hand-curated;
//   the listed model names work but aren't authoritative — users can
//   edit them in the table).
//
// modelFields: which columns the models-table renders for this provider.

const PROVIDER_FIELDS = {
  llm: {
    openresponses: {
      label: "OpenAI / OpenAI-compatible (openresponses)",
      config: [
        { key: "url", label: "Base URL", type: "url", placeholder: "https://api.openai.com/v1", required: true },
        { key: "api_key", label: "API key", type: "password", required: true },
        { key: "flavor", label: "Flavor", type: "enum", options: ["openai", "lmstudio", "other"], default: "other" },
      ],
      discoverable: true,
      suggestedModels: [
        { name: "gpt-4o", context_length: 128000 },
        { name: "gpt-4o-mini", context_length: 128000 },
        { name: "gpt-4-turbo", context_length: 128000 },
      ],
      modelFields: [
        { key: "name", label: "Model name", type: "text", flex: 2 },
        { key: "context_length", label: "Context", type: "number", flex: 1, min: 1 },
      ],
    },
    anthropic: {
      label: "Anthropic",
      config: [
        { key: "api_key", label: "API key", type: "password", required: true },
      ],
      discoverable: false,
      suggestedModels: [
        { name: "claude-opus-4-5", context_length: 200000 },
        { name: "claude-sonnet-4-5", context_length: 200000 },
        { name: "claude-haiku-4-5", context_length: 200000 },
      ],
      modelFields: [
        { key: "name", label: "Model name", type: "text", flex: 2 },
        { key: "context_length", label: "Context", type: "number", flex: 1, min: 1 },
      ],
    },
    gemini: {
      label: "Google Gemini",
      config: [
        { key: "api_key", label: "API key", type: "password", required: true },
      ],
      discoverable: false,
      suggestedModels: [
        { name: "gemini-2.5-pro", context_length: 2000000 },
        { name: "gemini-2.5-flash", context_length: 1000000 },
        { name: "gemini-2.0-flash", context_length: 1000000 },
      ],
      modelFields: [
        { key: "name", label: "Model name", type: "text", flex: 2 },
        { key: "context_length", label: "Context", type: "number", flex: 1, min: 1 },
      ],
    },
    ollama: {
      label: "Ollama",
      config: [
        { key: "url", label: "Base URL", type: "url", placeholder: "http://localhost:11434", required: true },
        { key: "api_key", label: "API key (optional)", type: "password" },
      ],
      discoverable: true,
      suggestedModels: [
        { name: "llama3.3:70b", context_length: 131072 },
        { name: "qwen3:8b", context_length: 32768 },
        { name: "gpt-oss:20b", context_length: 131072 },
      ],
      modelFields: [
        { key: "name", label: "Model name", type: "text", flex: 2 },
        { key: "context_length", label: "Context", type: "number", flex: 1, min: 1 },
      ],
    },
  },
  embedding: {
    openai: {
      label: "OpenAI / OpenAI-compatible",
      config: [
        { key: "url", label: "Base URL", type: "url", placeholder: "https://api.openai.com/v1", required: true },
        { key: "api_key", label: "API key", type: "password", required: true },
        { key: "flavor", label: "Flavor", type: "enum", options: ["openai", "lmstudio", "other"], default: "other" },
      ],
      discoverable: true,
      suggestedModels: [
        { name: "text-embedding-3-small" },
        { name: "text-embedding-3-large" },
      ],
      modelFields: [{ key: "name", label: "Model name", type: "text", flex: 1 }],
    },
    huggingface: {
      label: "HuggingFace (local sentence-transformers)",
      config: [
        { key: "token", label: "HF token", type: "password", required: true, help: "Required to pull the transformer model — even public models." },
      ],
      discoverable: false,
      suggestedModels: [
        { name: "BAAI/bge-large-en-v1.5" },
        { name: "BAAI/bge-base-en-v1.5" },
        { name: "sentence-transformers/all-MiniLM-L6-v2" },
        { name: "mixedbread-ai/mxbai-embed-large-v1" },
      ],
      modelFields: [{ key: "name", label: "Model name", type: "text", flex: 1 }],
    },
    gemini: {
      label: "Google Gemini",
      config: [
        { key: "api_key", label: "API key", type: "password", required: true },
      ],
      discoverable: false,
      suggestedModels: [
        { name: "gemini-embedding-001" },
        { name: "text-embedding-004" },
      ],
      modelFields: [{ key: "name", label: "Model name", type: "text", flex: 1 }],
    },
  },
  rerank: {
    huggingface: {
      label: "HuggingFace (local sentence-transformers)",
      config: [
        { key: "token", label: "HF token (optional for public repos)", type: "password" },
      ],
      discoverable: false,
      suggestedModels: [
        { name: "BAAI/bge-reranker-v2-m3" },
        { name: "cross-encoder/ms-marco-MiniLM-L-6-v2" },
        { name: "cross-encoder/ms-marco-MiniLM-L-12-v2" },
        { name: "mixedbread-ai/mxbai-rerank-large-v1" },
      ],
      modelFields: [
        { key: "name", label: "Model name", type: "text", flex: 2 },
        { key: "max_pair_length", label: "Max pair length", type: "number", flex: 1, min: 1 },
      ],
    },
  },
};

// Normalize the kind URL segment ("rerank" / "cross_encoder") to the
// PROVIDER_FIELDS key.
const _normKind = (k) => (k === "cross_encoder" ? "rerank" : k);

function NewProviderModal({ kindKey, plural, label, onClose, onCreate }) {
  const { push: pushToast } = useToast();
  const fieldKind = _normKind(kindKey);
  const providers = PROVIDER_FIELDS[fieldKind] || {};
  const providerOptions = Object.keys(providers);

  const [id, setId] = React.useState("");
  const [provider, setProvider] = React.useState(providerOptions[0] || "");
  const [configValues, setConfigValues] = React.useState({});
  const [models, setModels] = React.useState([]);
  const [maxConcurrency, setMaxConcurrency] = React.useState(1);
  const [fieldErrors, setFieldErrors] = React.useState({});

  // Whenever the provider type changes, re-seed config defaults +
  // wipe the models list (it would have the wrong shape).
  React.useEffect(() => {
    const def = providers[provider];
    if (!def) return;
    const seeded = {};
    for (const f of def.config) {
      if (f.default !== undefined) seeded[f.key] = f.default;
    }
    setConfigValues(seeded);
    setModels([]);
    setFieldErrors({});
  }, [provider]);  // eslint-disable-line react-hooks/exhaustive-deps

  const def = providers[provider];

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

  const discover = useMutation(
    (body) => apiFetch("POST", "/" + plural + "/_discover_models", body),
    {
      onSuccess: (r) => {
        if (Array.isArray(r?.models) && r.models.length > 0) {
          setModels(r.models.map((m) => ({ ...m })));
          pushToast({ kind: "success", title: "Models fetched", detail: `${r.models.length} model${r.models.length === 1 ? "" : "s"} from the provider.` });
        } else {
          pushToast({ kind: "warning", title: "Provider returned no models", detail: "Check the URL / credentials, or add models manually." });
        }
      },
      onError: (err) => pushToast({
        kind: "error",
        title: err.title || "Discovery failed",
        detail: err.detail || err.message,
        requestId: err.requestId,
      }),
    }
  );

  const handleFetchModels = () => {
    if (def?.discoverable) {
      discover.mutate({ provider, config: configValues });
    } else if (def?.suggestedModels?.length) {
      setModels(def.suggestedModels.map((m) => ({ ...m })));
      pushToast({ kind: "info", title: "Models populated", detail: "Suggested defaults loaded — edit names as needed; provider does not expose a live list endpoint." });
    }
  };

  const addModel = () => {
    if (!def) return;
    const empty = Object.fromEntries(def.modelFields.map((f) => [f.key, ""]));
    setModels((arr) => [...arr, empty]);
  };
  const removeModel = (i) => setModels((arr) => arr.filter((_, idx) => idx !== i));
  const updateModel = (i, patch) => setModels((arr) => arr.map((m, idx) => idx === i ? { ...m, ...patch } : m));

  // Strip empty optional fields from the config payload before submit
  // (e.g. an empty api_key on ollama should be omitted, not sent as "").
  const cleanConfig = () => {
    if (!def) return {};
    const out = {};
    for (const f of def.config) {
      const v = configValues[f.key];
      if (v !== undefined && v !== "" && v !== null) out[f.key] = v;
    }
    return out;
  };

  // Strip empty optional model fields (e.g. max_pair_length="").
  const cleanModels = () => models.map((m) => {
    const cleaned = {};
    for (const f of def.modelFields) {
      const v = m[f.key];
      if (v === undefined || v === "" || v === null) continue;
      cleaned[f.key] = f.type === "number" ? Number(v) : v;
    }
    return cleaned;
  });

  const submit = async () => {
    setFieldErrors({});
    const body = {
      ...(id ? { id } : {}),
      provider,
      config: cleanConfig(),
      models: cleanModels(),
      limits: { max_concurrency: Number(maxConcurrency) || 1 },
    };
    try { await create.mutate(body); } catch (_e) {}
  };

  const canSubmit = !!provider
    && models.length > 0
    && def?.config.every((f) => !f.required || (configValues[f.key] != null && configValues[f.key] !== ""))
    && !create.loading;

  return (
    <Modal
      title={`New ${label.toLowerCase()} provider`}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="plus" onClick={submit} disabled={!canSubmit}>
            {create.loading ? "Creating…" : "Create"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">ID <span className="hint">optional — backend assigns if blank</span></label>
        <input className="input" value={id} onChange={(e) => setId(e.target.value)} placeholder="auto-generated" style={{ width: "100%" }} />
        {fieldErrors["body.id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.id"]}</div>}
      </div>

      <div className="field">
        <label className="field-label">Provider</label>
        <select className="select" value={provider} onChange={(e) => setProvider(e.target.value)} style={{ width: "100%" }}>
          {providerOptions.map((p) => <option key={p} value={p}>{providers[p].label}</option>)}
        </select>
        {fieldErrors["body.provider"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.provider"]}</div>}
      </div>

      {def && def.config.map((f) => (
        <ConfigField
          key={f.key}
          field={f}
          value={configValues[f.key] ?? ""}
          onChange={(v) => setConfigValues((cv) => ({ ...cv, [f.key]: v }))}
          error={fieldErrors[`body.config.${f.key}`]}
        />
      ))}

      <div className="field">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <label className="field-label">Models</label>
          <div style={{ display: "flex", gap: 6 }}>
            <Btn
              size="sm"
              kind="ghost"
              icon="refresh"
              onClick={handleFetchModels}
              disabled={discover.loading || (def && !def.discoverable && !(def.suggestedModels?.length))}
              title={
                def?.discoverable
                  ? "Live-probe the provider for its model list"
                  : "Load curated suggestions (provider has no live list endpoint)"
              }
            >
              {discover.loading ? "Fetching…" : (def?.discoverable ? "Fetch models" : "Suggest models")}
            </Btn>
            <Btn size="sm" kind="ghost" icon="plus" onClick={addModel}>Add</Btn>
          </div>
        </div>
        {models.length === 0 && (
          <div className="field-help muted">— no models — add at least one before saving (or use the buttons above).</div>
        )}
        {def && models.map((m, i) => (
          <div key={i} style={{ display: "flex", gap: 6, marginTop: 4, alignItems: "center" }}>
            {def.modelFields.map((mf) => (
              <input
                key={mf.key}
                className="input mono"
                type={mf.type === "number" ? "number" : "text"}
                {...(mf.min != null ? { min: mf.min } : {})}
                value={m[mf.key] ?? ""}
                placeholder={mf.label}
                onChange={(e) => updateModel(i, { [mf.key]: e.target.value })}
                style={{ flex: mf.flex || 1 }}
              />
            ))}
            <Btn size="sm" kind="ghost" onClick={() => removeModel(i)} title="Remove">×</Btn>
          </div>
        ))}
        {fieldErrors["body.models"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.models"]}</div>}
      </div>

      <div className="field">
        <label className="field-label">Max concurrency <span className="hint">in-flight requests cap</span></label>
        <input
          className="input"
          type="number"
          min="1"
          value={maxConcurrency}
          onChange={(e) => setMaxConcurrency(e.target.value)}
          style={{ width: 120 }}
        />
        {fieldErrors["body.limits.max_concurrency"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.limits.max_concurrency"]}</div>}
      </div>
    </Modal>
  );
}

function ConfigField({ field, value, onChange, error }) {
  return (
    <div className="field">
      <label className="field-label">
        {field.label}
        {!field.required && <span className="hint">optional</span>}
      </label>
      {field.type === "enum" ? (
        <select className="select" value={value || field.default || ""} onChange={(e) => onChange(e.target.value)} style={{ width: "100%" }}>
          {field.options.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
        </select>
      ) : (
        <input
          className={`input ${field.type === "password" || field.type === "url" ? "mono" : ""}`}
          type={field.type === "password" ? "password" : "text"}
          value={value}
          placeholder={field.placeholder || ""}
          onChange={(e) => onChange(e.target.value)}
          style={{ width: "100%" }}
        />
      )}
      {field.help && <div className="field-help">{field.help}</div>}
      {error && <div className="field-help" style={{ color: "var(--red)" }}>{error}</div>}
    </div>
  );
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
