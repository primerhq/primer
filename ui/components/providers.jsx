/* global React, Icon, Btn, Modal, Banner, CardList, Card, Fab */

// NOTE: do NOT destructure window.primerApi at module top level. The docs
// embeds install a fixture-backed stub (DocsMakeStubApi) AFTER this module is
// evaluated, so a top-level capture would freeze the real (network) apiFetch.
// Each component reads from window.primerApi inside its own render instead.

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
  openchat: "var(--green)",
};

// kind prop ("llm" / "embedding" / "rerank") -> URL segment + REST plural +
// display label. The URL segment differs from the kind prop for rerank
// (URL uses "cross_encoder", kind prop stays "rerank" for back-compat
// with app.jsx and the sidebar nav).
const KINDS = {
  llm: { plural: "llm_providers", segment: "llm", label: "LLM" },
  embedding: { plural: "embedding_providers", segment: "embedding", label: "Embedding" },
  rerank: { plural: "cross_encoder_providers", segment: "cross_encoder", label: "Cross-Encoder" },
  cross_encoder: { plural: "cross_encoder_providers", segment: "cross_encoder", label: "Cross-Encoder" },
};

// ============================================================================
// PROVIDER_KINDS_FIELDS — single source of truth mirroring the backend's provider
// enums + per-provider Config models in primer/model/provider.py. Keep these
// in sync when the backend grows a new provider type.
//
// Each kind (llm / embedding / rerank) maps provider-type-string ->
//   { label, config: [field, ...], discoverable, suggestedModels, modelFields }
//
// field: { key, label, type, placeholder?, options?, required?, default? }
//   type: "text" | "password" | "url" | "enum"
//
// discoverable: when true, the "Fetch Models" button calls
//   POST /v1/{plural}/_discover_models with the draft {provider, config}
//   and replaces the models table with the response. When false, the button
//   populates the table from `suggestedModels` (hand-curated; the listed
//   model names work but aren't authoritative — users can edit them).
//
// modelFields: which columns the models-table renders for this provider.
// ============================================================================

const PROVIDER_KINDS_FIELDS = {
  llm: {
    openresponses: {
      label: "OpenAI / OpenAI-compatible (openresponses)",
      config: [
        { key: "url", label: "Base URL", type: "url", placeholder: "https://api.openai.com/v1", required: true },
        { key: "api_key", label: "API key (optional)", type: "password", help: "Required for real OpenAI; leave blank for LM Studio / vLLM / unauthenticated proxies." },
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
    openchat: {
      label: "OpenAI-compatible Chat Completions (openchat)",
      config: [
        { key: "url", label: "Base URL", type: "url", placeholder: "https://api.openai.com/v1", required: true },
        { key: "api_key", label: "API key (optional)", type: "password", help: "Required for real OpenAI / OpenRouter / Together; leave blank for LM Studio / Ollama / vLLM / unauthenticated proxies." },
        { key: "flavor", label: "Flavor", type: "enum", options: ["openai", "lmstudio", "ollama", "vllm", "other"], default: "other" },
      ],
      discoverable: true,
      suggestedModels: [
        { name: "gpt-4o-mini", context_length: 128000 },
        { name: "gpt-4o", context_length: 128000 },
        { name: "local-model", context_length: 8192 },
      ],
      modelFields: [
        { key: "name", label: "Model name", type: "text", flex: 2 },
        { key: "context_length", label: "Context", type: "number", flex: 1, min: 1 },
      ],
    },
    anthropic: {
      label: "Anthropic",
      config: [
        { key: "api_key", label: "API key (optional)", type: "password", help: "Required for the real Anthropic API; leave blank only when an upstream proxy supplies auth." },
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
        { key: "api_key", label: "API key (optional)", type: "password", help: "Required for the real Gemini API; leave blank only when an upstream proxy supplies auth." },
      ],
      discoverable: true,
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
    openrouter: {
      label: "OpenRouter",
      config: [
        { key: "api_key", label: "API key", type: "password", required: true,
          help: "OpenRouter API key. Required; the upstream is always remote and always authenticated." },
        { key: "app_name", label: "App name (optional)", type: "text",
          placeholder: "primer-staging",
          help: "Sent as X-Title for OpenRouter app attribution. Optional." },
        { key: "app_url", label: "App URL (optional)", type: "url",
          placeholder: "https://primer.example",
          help: "Sent as HTTP-Referer for OpenRouter attribution. Optional." },
      ],
      discoverable: true,
      suggestedModels: [
        { name: "anthropic/claude-3.5-sonnet", context_length: 200000 },
        { name: "openai/gpt-4o", context_length: 128000 },
        { name: "google/gemini-2.5-pro", context_length: 2000000 },
      ],
      modelFields: [
        { key: "name", label: "Model name", type: "text", flex: 2 },
        { key: "context_length", label: "Context", type: "number", flex: 1, min: 1 },
      ],
      // OpenRouter-specific picker hint; consumed by the picker
      // component in Task 5.2 to render the richer variant.
      pickerVariant: "openrouter",
    },
  },
  embedding: {
    openai: {
      label: "OpenAI / OpenAI-compatible",
      config: [
        { key: "url", label: "Base URL", type: "url", placeholder: "https://api.openai.com/v1", required: true },
        { key: "api_key", label: "API key (optional)", type: "password", help: "Required for real OpenAI; leave blank for LM Studio / vLLM / unauthenticated proxies." },
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
        { key: "api_key", label: "API key (optional)", type: "password", help: "Required for the real Gemini API; leave blank only when an upstream proxy supplies auth." },
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

// Normalize the kind prop / URL segment to the PROVIDER_KINDS_FIELDS key.
const _normKind = (k) => (k === "cross_encoder" ? "rerank" : k);

// OpenRouter slug shape: provider/model[:tag]. Used by the rich picker's
// "Add by id" input to reject obvious typos before they hit the backend.
const OPENROUTER_SLUG_RE = /^[a-z0-9-]+\/[a-z0-9._-]+(:[a-z0-9-]+)?$/;
// Fallback context length when the operator adds an id manually and we
// don't have a discovered row to source it from. 128k is a safe lower
// bound for almost every OpenRouter-routed model in 2026; the operator
// can always tighten it in the per-row inputs below the picker.
const OPENROUTER_DEFAULT_LLM_CONTEXT = 128000;

// ============================================================================
// Top-level page — list view vs detail view dispatched on the router's id.
// ============================================================================

function ProvidersPage({ kind: kindProp, pushToast }) {
  const { useRouter } = window.primerApi;
  const { params } = useRouter();
  const k = KINDS[kindProp];
  if (!k) return <Banner kind="error" title="Unknown provider kind" detail={String(kindProp)} />;

  if (params.id) {
    return <ProviderDetail kindProp={kindProp} id={params.id} pushToast={pushToast} />;
  }
  return <ProvidersList kindProp={kindProp} pushToast={pushToast} />;
}

// ============================================================================
// List
// ============================================================================

function ProvidersList({ kindProp, pushToast }) {
  const { apiFetch, useResource, useViewport } = window.primerApi;
  const k = KINDS[kindProp];
  const { isMobile } = useViewport();
  const list = useResource(
    `providers:${k.plural}`,
    (s) => apiFetch("GET", "/" + k.plural + "?limit=200", null, { signal: s }),
    {},
  );
  const [createOpen, setCreateOpen] = React.useState(false);
  const [textFilter, setTextFilter] = React.useState("");

  const items = list.data?.items ?? [];
  const filtered = items.filter((p) => !textFilter || (p.id || "").toLowerCase().includes(textFilter.toLowerCase()));

  const navigateDetail = (id) => { window.location.hash = "#/providers/" + k.segment + "/" + encodeURIComponent(id); };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="filter-bar">
        <div className="input-icon">
          <Icon name="search" size={13} className="icon" />
          <input className="input" placeholder="Filter providers…" value={textFilter} onChange={(e) => setTextFilter(e.target.value)} />
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={list.refetch}>Refresh</Btn>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New {k.label.toLowerCase()} provider</Btn>
        </div>
      </div>

      {isMobile ? (
        list.loading && items.length === 0 ? (
          <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
        ) : list.error && items.length === 0 ? (
          <Banner
            kind="error"
            title={list.error.title || "Couldn't load providers"}
            detail={list.error.detail || list.error.message}
            actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>}
          />
        ) : (
          <CardList
            items={filtered}
            empty={items.length === 0
              ? `No ${k.label.toLowerCase()} providers yet.`
              : "No providers match."}
            renderCard={(p) => {
              const color = VENDOR_COLORS[p.provider] || "var(--text-3)";
              const modelCount = (p.models || []).length;
              return (
                <Card
                  title={p.id}
                  subtitle={
                    <span className="mono">
                      <span className="dot" style={{ background: color, marginRight: 6, display: "inline-block", width: 8, height: 8, borderRadius: "50%" }}></span>
                      {p.provider || "—"}
                    </span>
                  }
                  meta={`${modelCount} model${modelCount === 1 ? "" : "s"}`}
                  onClick={() => navigateDetail(p.id)}
                />
              );
            }}
          />
        )
      ) : (
      <div className="tbl-wrap">
        <table className="tbl">
          <thead>
            <tr>
              <th>ID</th>
              <th>Provider</th>
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
                <tr key={p.id} onClick={() => navigateDetail(p.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{p.id}</td>
                  <td className="mono">
                    <span className="dot" style={{ background: color, marginRight: 6, display: "inline-block", width: 8, height: 8, borderRadius: "50%" }}></span>
                    {p.provider || "—"}
                  </td>
                  <td className="mono num tabular" style={{ textAlign: "right" }}>{modelCount}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      )}

      {isMobile && (
        <Fab icon="plus" label="New provider" onClick={() => setCreateOpen(true)} />
      )}

      {createOpen && (
        <NewProviderModal
          kindProp={kindProp}
          plural={k.plural}
          label={k.label}
          onClose={() => setCreateOpen(false)}
          pushToast={pushToast}
          onCreated={(row) => {
            setCreateOpen(false);
            if (pushToast) pushToast({ kind: "success", title: "Provider created", detail: row.id });
            list.refetch();
            navigateDetail(row.id);
          }}
        />
      )}
    </div>
  );
}

// ============================================================================
// OpenRouterModelPicker — rich picker activated when the form's
// pickerVariant is "openrouter". Reads the discovered catalogue (rich
// rows with id, name, context_length, pricing, modality), provides a
// debounced filter + 50-row paged grid + Add-by-id input. Selection
// state lives on the parent form (the standard `models` array of
// {name, context_length}); this component only fires onSelect /
// onDeselect callbacks to mutate it.
// ============================================================================

function OpenRouterModelPicker({ discovered, selected, onSelect, onDeselect }) {
  const [q, setQ] = React.useState("");
  const [debouncedQ, setDebouncedQ] = React.useState("");
  const [page, setPage] = React.useState(0);
  const [byIdInput, setByIdInput] = React.useState("");
  const [byIdError, setByIdError] = React.useState(null);
  const PAGE_SIZE = 50;

  // Debounce filter input by 200ms so a fast typist doesn't trigger a
  // re-render and slice() on every keystroke against a ~300-row catalogue.
  React.useEffect(() => {
    const t = setTimeout(() => setDebouncedQ(q), 200);
    return () => clearTimeout(t);
  }, [q]);

  const selectedIds = React.useMemo(
    () => new Set((selected || []).map((m) => m.name)),
    [selected],
  );

  const filtered = React.useMemo(() => {
    const ql = (debouncedQ || "").toLowerCase().trim();
    if (!ql) return discovered;
    return discovered.filter((row) =>
      (row.id || "").toLowerCase().includes(ql)
      || (row.name || "").toLowerCase().includes(ql)
    );
  }, [discovered, debouncedQ]);

  const total = filtered.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const safePage = Math.min(page, pages - 1);
  const start = safePage * PAGE_SIZE;
  const rows = filtered.slice(start, start + PAGE_SIZE);

  // Reset to page 1 whenever the filter changes, otherwise a filter that
  // collapses the result set to <safePage*50 rows lands on an empty page.
  React.useEffect(() => { setPage(0); }, [debouncedQ]);

  const addById = () => {
    const slug = byIdInput.trim();
    if (!OPENROUTER_SLUG_RE.test(slug)) {
      setByIdError("Invalid slug. Expected provider/model[:tag].");
      return;
    }
    if (selectedIds.has(slug)) {
      setByIdError("Already selected.");
      return;
    }
    onSelect({
      id: slug,
      name: slug,
      context_length: OPENROUTER_DEFAULT_LLM_CONTEXT,
    });
    setByIdInput("");
    setByIdError(null);
  };

  return (
    <div style={{
      marginTop: 8,
      border: "1px solid var(--border)",
      borderRadius: 6,
      padding: 10,
    }}>
      <div style={{
        display: "flex", gap: 8, alignItems: "center", marginBottom: 8,
      }}>
        <input
          className="input"
          placeholder="Filter (id, name, provider prefix)…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ flex: 1, fontSize: 12 }}
        />
        <span className="muted text-sm">
          Selected: {selectedIds.size}
        </span>
      </div>
      <div style={{
        maxHeight: 320,
        overflow: "auto",
        border: "1px solid var(--border)",
        borderRadius: 4,
      }}>
        {rows.map((row) => {
          const isSel = selectedIds.has(row.id);
          return (
            <div
              key={row.id}
              style={{
                display: "grid",
                gridTemplateColumns: "20px 1.5fr 1fr 1fr 80px",
                alignItems: "center",
                gap: 8,
                padding: "6px 10px",
                borderBottom: "1px solid var(--border)",
                fontSize: 12,
                background: isSel ? "var(--bg-2)" : "transparent",
                cursor: "pointer",
              }}
              onClick={() => isSel ? onDeselect(row.id) : onSelect(row)}
            >
              <input type="checkbox" checked={isSel} readOnly />
              <div>
                <div style={{ fontFamily: "var(--mono)" }}>{row.id}</div>
                <div className="muted text-sm">{row.name}</div>
              </div>
              <div className="muted text-sm">{row.context_length ?? "?"} ctx</div>
              <div className="muted text-sm">
                ${row.input_price_per_million ?? "?"} / ${row.output_price_per_million ?? "?"} per M
              </div>
              <div className="muted text-sm">{row.modality || "text"}</div>
            </div>
          );
        })}
      </div>
      <div style={{
        display: "flex",
        gap: 8,
        alignItems: "center",
        marginTop: 8,
      }}>
        <Btn
          size="sm"
          kind="ghost"
          onClick={() => setPage(Math.max(0, safePage - 1))}
          disabled={safePage === 0}
        >
          ← Prev
        </Btn>
        <span className="muted text-sm">
          Page {safePage + 1} of {pages} · {total} models
        </span>
        <Btn
          size="sm"
          kind="ghost"
          onClick={() => setPage(Math.min(pages - 1, safePage + 1))}
          disabled={safePage >= pages - 1}
        >
          Next →
        </Btn>
      </div>
      <div style={{
        display: "flex",
        gap: 6,
        alignItems: "center",
        marginTop: 12,
      }}>
        <input
          className="input mono"
          placeholder="Add by id: anthropic/claude-3.5-sonnet"
          value={byIdInput}
          onChange={(e) => { setByIdInput(e.target.value); setByIdError(null); }}
          style={{ flex: 1, fontSize: 12 }}
        />
        <Btn size="sm" kind="primary" onClick={addById}>Add</Btn>
      </div>
      {byIdError && (
        <div style={{ color: "var(--red)", fontSize: 11, marginTop: 4 }}>
          {byIdError}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Create modal — provider-pattern with rich per-provider controls.
// ============================================================================

function NewProviderModal({ kindProp, plural, label, onClose, onCreated, pushToast, existing }) {
  const { apiFetch, useMutation } = window.primerApi;
  const _push = pushToast || (() => {});
  const fieldKind = _normKind(kindProp);
  const providers = PROVIDER_KINDS_FIELDS[fieldKind] || {};
  const providerOptions = Object.keys(providers);
  // Same modal handles create (existing == null) and edit. In edit
  // mode the id and provider-type are locked, secrets get a
  // placeholder hint, and submit PUT-replaces.
  const isEdit = !!existing;

  const [id, setId] = React.useState(existing?.id || "");
  const [provider, setProvider] = React.useState(
    existing?.provider || providerOptions[0] || ""
  );
  // Secret fields from the GET row arrive redacted as "**********".
  // Persisting that literal would clobber the real secret, so prefill
  // edit-mode secrets as blank and force the operator to re-enter
  // them (or leave blank — at submit we still pass blank, which the
  // form-validity check forbids for required secrets, surfacing the
  // requirement up-front).
  const [configValues, setConfigValues] = React.useState(() => {
    if (!isEdit) return {};
    const seed = { ...(existing?.config || {}) };
    const def0 = providers[existing?.provider];
    if (def0) {
      for (const f of def0.config) {
        if (f.type === "password" && /^\*{6,}$/.test(String(seed[f.key] || ""))) {
          seed[f.key] = "";
        }
      }
    }
    return seed;
  });
  const [models, setModels] = React.useState(
    () => isEdit ? (existing?.models || []).map((m) => ({ ...m })) : []
  );
  // Rich catalogue from a discovery probe — populated only for providers
  // whose pickerVariant routes through OpenRouterModelPicker (currently
  // just "openrouter"). For other providers discovery still flows into
  // `models` directly via the legacy onSuccess branch below.
  const [discovered, setDiscovered] = React.useState([]);
  const [maxConcurrency, setMaxConcurrency] = React.useState(
    existing?.limits?.max_concurrency ?? 1
  );
  const [requestTimeoutSeconds, setRequestTimeoutSeconds] = React.useState(
    existing?.limits?.request_timeout_seconds !== undefined
      ? String(existing.limits.request_timeout_seconds ?? "")
      : "300"
  );
  const [fieldErrors, setFieldErrors] = React.useState({});

  // Whenever the provider type changes, re-seed config defaults + wipe the
  // models list (it would have the wrong shape). Skipped in edit mode
  // on the FIRST render — we already seeded from the existing row, and
  // resetting would discard the live config. We only re-seed if the
  // user manually changes provider (which is also locked in edit).
  const _isFirstRender = React.useRef(true);
  React.useEffect(() => {
    if (isEdit && _isFirstRender.current) {
      _isFirstRender.current = false;
      return;
    }
    _isFirstRender.current = false;
    const def = providers[provider];
    if (!def) return;
    const seeded = {};
    for (const f of def.config) {
      if (f.default !== undefined) seeded[f.key] = f.default;
    }
    setConfigValues(seeded);
    setModels([]);
    setDiscovered([]);
    setFieldErrors({});
  }, [provider]); // eslint-disable-line react-hooks/exhaustive-deps

  const def = providers[provider];

  const create = useMutation(
    (body) => isEdit
      ? apiFetch("PUT", "/" + plural + "/" + encodeURIComponent(existing.id), body)
      : apiFetch("POST", "/" + plural, body),
    {
      invalidates: isEdit
        ? [`providers:${plural}`, `provider-detail:${plural}:${existing?.id}`]
        : [`providers:${plural}`],
      onSuccess: (row) => onCreated(row),
      onError: (err) => {
        if (err.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) next[(fe.loc || []).join(".")] = fe.msg;
          setFieldErrors(next);
        } else {
          _push({
            kind: "error",
            title: err.title || (isEdit ? "Save failed" : "Create failed"),
            detail: err.detail || err.message,
            requestId: err.requestId,
          });
        }
      },
    },
  );

  const discover = useMutation(
    (body) => apiFetch("POST", "/" + plural + "/_discover_models", body),
    {
      onSuccess: (r) => {
        if (Array.isArray(r?.models) && r.models.length > 0) {
          // For pickerVariant === "openrouter" the response is a rich
          // catalogue (pricing, modality, etc.) and we don't auto-select
          // hundreds of rows — we hand it to OpenRouterModelPicker and
          // let the operator pick. For every other provider we keep the
          // legacy behaviour where discovered rows go straight into the
          // models table.
          const curDef = providers[provider];
          if (curDef?.pickerVariant === "openrouter") {
            setDiscovered(r.models.map((m) => ({ ...m })));
          } else {
            setModels(r.models.map((m) => ({ ...m })));
          }
          _push({
            kind: "success",
            title: "Models fetched",
            detail: `${r.models.length} model${r.models.length === 1 ? "" : "s"} from the provider.`,
          });
        } else {
          _push({
            kind: "warning",
            title: "Provider returned no models",
            detail: "Check the URL / credentials, or add models manually.",
          });
        }
      },
      onError: (err) => _push({
        kind: "error",
        title: err.title || "Discovery failed",
        detail: err.detail || err.message,
        requestId: err.requestId,
      }),
    },
  );

  const handleFetchModels = () => {
    if (def?.discoverable) {
      discover.mutate({ provider, config: configValues });
    } else if (def?.suggestedModels?.length) {
      setModels(def.suggestedModels.map((m) => ({ ...m })));
      _push({
        kind: "info",
        title: "Models populated",
        detail: "Suggested defaults loaded — edit names as needed; provider does not expose a live list endpoint.",
      });
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
  // In edit mode, secret fields prefilled with the "**********" redaction
  // are also stripped — the backend must keep the existing value, not
  // overwrite with the literal asterisks.
  const _isRedaction = (v) => typeof v === "string" && /^\*{6,}$/.test(v);
  const cleanConfig = () => {
    if (!def) return {};
    const out = {};
    for (const f of def.config) {
      const v = configValues[f.key];
      if (v === undefined || v === "" || v === null) continue;
      if (isEdit && _isRedaction(v)) continue;  // operator left secret unchanged
      out[f.key] = v;
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
      // PUT-replace requires id; create allows empty so the backend
      // can autogenerate.
      ...(isEdit ? { id: existing.id } : (id ? { id } : {})),
      provider,
      config: cleanConfig(),
      models: cleanModels(),
      limits: {
        max_concurrency: Number(maxConcurrency) || 1,
        ...(requestTimeoutSeconds !== "" && requestTimeoutSeconds !== null
          ? { request_timeout_seconds: requestTimeoutSeconds === "null" ? null : Number(requestTimeoutSeconds) }
          : {}),
      },
    };
    try { await create.mutate(body); } catch (_e) { /* handled in onError */ }
  };

  const canSubmit = !!provider
    && models.length > 0
    && def?.config.every((f) => !f.required || (configValues[f.key] != null && configValues[f.key] !== ""))
    && !create.loading;

  return (
    <Modal
      title={isEdit
        ? `Edit ${label.toLowerCase()} provider · ${existing.id}`
        : `New ${label.toLowerCase()} provider`}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "check" : "plus"} onClick={submit} disabled={!canSubmit}>
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
        <label className="field-label">Provider {isEdit && <span className="hint">locked — recreate to change provider type</span>}</label>
        <select className="select" value={provider} onChange={(e) => setProvider(e.target.value)} disabled={isEdit} style={{ width: "100%" }}>
          {providerOptions.map((p) => <option key={p} value={p}>{providers[p].label}</option>)}
        </select>
        {/* T0379: documented anomaly surface — see docs/testing/05-ui-spec.md §5.
            The backend does NOT cross-validate that the chosen `provider`
            type and the supplied `config` shape agree (it'll happily persist
            a row with mismatched provider + config and only surface the bug
            at adapter-construction time on first use). Surface this
            asymmetry on the form so operators don't ship a misaligned row. */}
        <div className="field-help">
          Provider ↔ config alignment is NOT cross-validated server-side (T0379) — make sure the provider type above matches the config shape below.
        </div>
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
        {def && def.pickerVariant === "openrouter" && discovered.length > 0 && (
          <OpenRouterModelPicker
            discovered={discovered}
            selected={models}
            onSelect={(row) => {
              const slug = row.id || row.name;
              if (models.some((m) => m.name === slug)) return;
              setModels([
                ...models,
                {
                  name: slug,
                  context_length: row.context_length || OPENROUTER_DEFAULT_LLM_CONTEXT,
                },
              ]);
            }}
            onDeselect={(slug) => {
              setModels(models.filter((m) => m.name !== slug));
            }}
          />
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
        {/* T0025: documented anomaly surface — see docs/testing/05-ui-spec.md §5.
            The /v1/{plural}/{id}/models endpoint returns this stored list,
            NOT a live provider probe. The Fetch/Suggest button above is the
            UI's escape hatch; this helper makes the static-list semantics
            explicit so operators don't expect the list to refresh on its own. */}
        <div className="field-help">
          Model list comes from the provider row, not a live introspection (T0025).
          Use the {def?.discoverable ? "Fetch" : "Suggest"} models button above to populate or refresh.
        </div>
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

      <div className="field">
        <label className="field-label">
          Stream inactivity timeout <span className="hint">seconds; blank or 0 to disable</span>
        </label>
        <input
          className="input"
          type="number"
          min="0"
          step="1"
          value={requestTimeoutSeconds}
          onChange={(e) => setRequestTimeoutSeconds(e.target.value)}
          style={{ width: 120 }}
          placeholder="300"
        />
        <div className="field-help">
          Maximum seconds to wait for the next streamed event from the provider.
          If no event arrives within this window the stream is aborted and the
          turn fails cleanly (slot released). This is a per-event stall timeout,
          not a total-generation cap -- long but progressing responses are not
          killed. Default 300 s. LM Studio / local models: lower to 60 s for
          faster failure detection if your hardware is fast enough.
        </div>
        {fieldErrors["body.limits.request_timeout_seconds"] && (
          <div className="field-help" style={{ color: "var(--red)" }}>
            {fieldErrors["body.limits.request_timeout_seconds"]}
          </div>
        )}
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

function ProviderDetail({ kindProp, id, pushToast }) {
  const { apiFetch, useResource, useMutation } = window.primerApi;
  const _push = pushToast || (() => {});
  const k = KINDS[kindProp];
  const listHash = "#/providers/" + k.segment;
  const goToList = () => { window.location.hash = listHash; };

  const detail = useResource(
    `provider-detail:${k.plural}:${id}`,
    (s) => apiFetch("GET", "/" + k.plural + "/" + encodeURIComponent(id), null, { signal: s }),
    { deps: [k.plural, id] },
  );

  const models = useResource(
    `provider-models:${k.plural}:${id}`,
    (s) => apiFetch("GET", "/" + k.plural + "/" + encodeURIComponent(id) + "/models", null, { signal: s }),
    { deps: [k.plural, id] },
  );

  const [confirmDelete, setConfirmDelete] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);

  const invalidate = useMutation(
    () => apiFetch("POST", "/" + k.plural + "/" + encodeURIComponent(id) + "/invalidate"),
    {
      invalidates: [`provider-detail:${k.plural}:${id}`],
      onSuccess: () => _push({ kind: "info", title: "Cache dropped", detail: id }),
      onError: (err) => _push({
        kind: "error",
        title: err.title || "Invalidate failed",
        detail: err.detail || err.message,
        requestId: err.requestId,
      }),
    },
  );

  const del = useMutation(
    () => apiFetch("DELETE", "/" + k.plural + "/" + encodeURIComponent(id)),
    {
      invalidates: [`providers:${k.plural}`],
      onSuccess: () => {
        _push({ kind: "warning", title: "Provider deleted", detail: id });
        goToList();
      },
      onError: (err) => {
        if (err.status === 409) {
          // Cascade conflict — surface inline (modal still open) so the
          // operator can see which downstream rows still reference this id.
          setDeleteError(err);
        } else {
          _push({
            kind: "error",
            title: err.title || "Delete failed",
            detail: err.detail || err.message,
            requestId: err.requestId,
          });
        }
      },
    },
  );

  return (
    <div className="col" style={{ gap: 14 }}>
      <ProviderDetailHeader label={k.label} segment={k.segment} id={id} onBack={goToList}
        onInvalidate={detail.data ? () => invalidate.mutate() : null}
        onDelete={detail.data ? () => { setDeleteError(null); setConfirmDelete(true); } : null}
      />

      {detail.loading && !detail.data ? (
        <div className="muted text-sm" style={{ padding: 40, textAlign: "center" }}>Loading…</div>
      ) : detail.error && !detail.data ? (
        <Banner
          kind="error"
          title={detail.error.title || "Couldn't load provider"}
          detail={detail.error.detail || detail.error.message}
          actions={<Btn size="sm" icon="chevron-left" onClick={goToList}>Back to list</Btn>}
        />
      ) : (
        <ProviderDetailBody p={detail.data} models={models} k={{ ...k, kindProp }} pushToast={pushToast} />
      )}

      {confirmDelete && (
        <Modal
          title={`Delete ${id}?`}
          danger
          onClose={() => setConfirmDelete(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setConfirmDelete(false)}>Cancel</Btn>
              <Btn kind="danger" icon="trash" onClick={async () => {
                try {
                  await del.mutate();
                  setConfirmDelete(false);
                } catch (_e) { /* onError handled inline above */ }
              }}>Delete</Btn>
            </>
          }
        >
          <ul>
            <li>Removes the provider row from storage.</li>
            <li>Any agent / collection referencing this provider id will fail at next use.</li>
            <li>Invalidates the backend's cached adapter (DELETE flushes the workspace registry side).</li>
            <li>DELETE is NOT idempotent on entities — a second DELETE returns 404 (app spec §5).</li>
          </ul>
          {deleteError && (
            <Banner
              kind="error"
              title={deleteError.title || "Delete blocked"}
              detail={deleteError.detail || deleteError.message}
            />
          )}
        </Modal>
      )}
    </div>
  );
}

function ProviderDetailHeader({ label, segment, id, onBack, onInvalidate, onDelete }) {
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="crumb">
          <span>Providers</span><span className="sep">/</span>
          <a onClick={onBack}>{label}</a>
          <span className="sep">/</span>
          <span className="mono" style={{ color: "var(--text)" }}>{id}</span>
        </div>
        <h1 className="page-title mono">{id}</h1>
      </div>
      <div className="page-actions">
        {onInvalidate && <Btn icon="refresh" kind="ghost" onClick={onInvalidate}>Invalidate</Btn>}
        {onDelete && <Btn icon="trash" kind="danger" onClick={onDelete}>Delete</Btn>}
        <Btn icon="chevron-left" kind="ghost" onClick={onBack}>Back</Btn>
      </div>
    </div>
  );
}

function ProviderDetailBody({ p, models, k, pushToast }) {
  const color = VENDOR_COLORS[p.provider] || "var(--text-3)";
  const modelList = models.data?.models ?? (p.models || []);

  const redactedPretty = React.useMemo(
    () => JSON.stringify(_redactSecrets(p), null, 2),
    [p],
  );

  // The provider row in `p` carries secrets redacted as "**********".
  // When the operator opens the form modal, we prefill from this row;
  // they must replace any "**********" entry with the real secret
  // before submit. The modal already renders the SecretStr fields as
  // password inputs so the placeholder shows but isn't permanent.
  const [editing, setEditing] = React.useState(false);

  return (
    <>
      <div className="panel">
        <div className="panel-h">
          <span className="dot" style={{ background: color, display: "inline-block", width: 8, height: 8, borderRadius: "50%" }}></span>
          <span className="mono">{p.id}</span>
          <span className="sub mono">· {p.provider}</span>
          <div className="right" style={{ display: "flex", gap: 6 }}>
            <Btn size="sm" icon="edit" kind="secondary" onClick={() => setEditing(true)}>Edit</Btn>
          </div>
        </div>
        <div className="panel-body">
          <div className="muted text-sm mb-3">
            PUT-replace edit. Secret fields are redacted as <span className="mono">"**********"</span> in the read-only view below; click Edit to update them through the form.
          </div>
          <pre className="code-block mono" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0 }}>
            {redactedPretty}
          </pre>
        </div>
      </div>

      {editing && (
        <NewProviderModal
          kindProp={k.kindProp}
          plural={k.plural}
          label={k.label}
          existing={p}
          pushToast={pushToast}
          onClose={() => setEditing(false)}
          onCreated={() => {
            setEditing(false);
            if (typeof pushToast === "function") {
              pushToast({ kind: "info", title: "Provider updated", detail: p.id });
            }
          }}
        />
      )}

      <div className="panel">
        <div className="panel-h">
          <Icon name={k.plural === "llm_providers" ? "llm" : "emb"} size={13} className="muted" />
          <span>Models</span>
          <span className="sub">· returns the static row list, not a live introspection (T0025)</span>
          <div className="right">
            <Btn size="sm" icon="refresh" kind="ghost" onClick={models.refetch} disabled={models.loading}>Refresh</Btn>
          </div>
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          {models.loading && modelList.length === 0 ? (
            <div className="muted text-sm" style={{ padding: 20, textAlign: "center" }}>Loading…</div>
          ) : models.error && (!models.data || modelList.length === 0) ? (
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
    </>
  );
}

function _redactSecrets(row) {
  // The backend already masks SecretStr fields on the wire (per app spec §7),
  // so this is defensive belt-and-braces in case a future mistake echoes them.
  // Scan for any string field named like "api_key" / "password" / "secret" /
  // "token" and stringify as "•••• (set)".
  const SECRET_RE = /(api_key|password|secret|token)/i;
  function walk(v) {
    if (v == null || typeof v !== "object") return v;
    if (Array.isArray(v)) return v.map(walk);
    const out = {};
    for (const [key, val] of Object.entries(v)) {
      if (typeof val === "string" && SECRET_RE.test(key) && val && !val.startsWith("•")) {
        out[key] = "•••• (redacted by UI)";
      } else {
        out[key] = walk(val);
      }
    }
    return out;
  }
  return walk(row);
}

// ============================================================================
// JsonField — textarea for JSON config blobs with a mobile "expand to full
// screen" affordance. On desktop it's a plain textarea; on mobile (or any
// viewport once expanded) we mount a full-screen overlay so the operator can
// edit multi-line JSON without fighting the on-screen keyboard / small box.
//
// No providers in this file currently expose raw JSON config (they all use
// per-field structured inputs), but the helper is here so future providers
// — and the matching test contract — have a single primitive to reach for.
// ============================================================================

function JsonField({ label, value, onChange, placeholder, rows = 6 }) {
  const { useViewport } = window.primerApi;
  const [expanded, setExpanded] = React.useState(false);
  const { isMobile } = useViewport();
  return (
    <div className="field">
      <label className="field-label">{label}</label>
      <textarea
        className="input mono"
        rows={rows}
        value={value}
        placeholder={placeholder || ""}
        onChange={(e) => onChange(e.target.value)}
        style={{ width: "100%", resize: "vertical" }}
      />
      {isMobile && (
        <button
          type="button"
          className="json-expand"
          onClick={() => setExpanded(true)}
          title="Expand to full screen"
        >
          Expand to full screen
        </button>
      )}
      {expanded && (
        <div className="json-expand-overlay">
          <div className="json-expand-header">
            <span className="mono">{label}</span>
            <Btn size="sm" kind="ghost" onClick={() => setExpanded(false)}>Done</Btn>
          </div>
          <textarea
            className="input mono json-expand-textarea"
            value={value}
            placeholder={placeholder || ""}
            onChange={(e) => onChange(e.target.value)}
            autoFocus
          />
        </div>
      )}
    </div>
  );
}

window.ProvidersPage = ProvidersPage;
window.ProviderDetail = ProviderDetail;
window.JsonField = JsonField;
