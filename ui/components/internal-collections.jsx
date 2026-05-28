/* global React, Icon, Btn, Modal, Banner, relativeTime */

// Internal Collections subsystem — wired to the real API.
//
// 3-state state machine, server-derived (no localStorage):
//   - inactive   : GET /internal_collections/config returns 404
//   - configured : 200 with `activated_at == null` (bootstrap required)
//   - active     : 200 with `activated_at` set
//
// Endpoints:
//   GET    /internal_collections/config       (404 → OFF, 200 → configured/active)
//   PUT    /internal_collections/config       (activate / re-configure)
//   POST   /internal_collections/bootstrap    (populate vector store, idempotent)
//   DELETE /internal_collections/config       (deactivate; data preserved)
//
// Cache keys:
//   ic:config                — main probe
//   ic:embedding-providers   — picker source in ConfigureModal
//   ic:rerank-providers      — optional cross-encoder picker
//   ic:ssp                   — required search_provider_id picker
//
// Babel-standalone shares the global scope across <script> tags so every
// top-level binding is prefixed with IC_ to avoid name clashes with other
// components (TS_*, KN_*, WS_*, etc.).

const IC_CACHE_CONFIG = "ic:config";
const IC_CACHE_EMBED = "ic:embedding-providers";
const IC_CACHE_RERANK = "ic:rerank-providers";
const IC_CACHE_SSP = "ic:ssp";

// 404 → null suppression for the IC config probe.
async function _icFetchConfig(signal) {
  const { apiFetch } = window.primerApi;
  try {
    return await apiFetch("GET", "/internal_collections/config", null, { signal });
  } catch (err) {
    if (err && err.status === 404) return null;
    throw err;
  }
}

function _icToastErr(pushToast, fallbackTitle) {
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
// Root page
// ============================================================================

function InternalCollectionsPage(props) {
  const { useResource, useToast } = window.primerApi;
  const toast = useToast ? useToast() : null;
  // Prefer the prop forwarded from app.jsx; fall back to the foundation
  // useToast hook so the component still works standalone.
  const pushToast = props && typeof props.pushToast === "function"
    ? props.pushToast
    : (toast?.push || (() => {}));

  const ic = useResource(IC_CACHE_CONFIG, _icFetchConfig, { pollMs: 30000 });

  const state = ic.data == null
    ? "inactive"
    : ic.data.activated_at
      ? "active"
      : "configured";

  return (
    <div className="col" style={{ gap: 14 }}>
      {state === "inactive" && <InactiveCard onRefresh={ic.refetch} pushToast={pushToast} />}
      {state === "configured" && <ConfiguredCard config={ic.data} onRefresh={ic.refetch} pushToast={pushToast} />}
      {state === "active" && <ActiveCard config={ic.data} onRefresh={ic.refetch} pushToast={pushToast} />}

      {ic.error && (
        <Banner
          kind="error"
          title={ic.error.title || "Couldn't load subsystem config"}
          detail={ic.error.detail || ic.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={ic.refetch}>Retry</Btn>}
        />
      )}
    </div>
  );
}

// ============================================================================
// State 1 — Inactive (404)
// ============================================================================

function InactiveCard({ onRefresh, pushToast }) {
  const [configureOpen, setConfigureOpen] = React.useState(false);
  return (
    <>
      <div className="panel">
        <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 24, padding: "30px 24px" }}>
          <div style={{
            width: 72, height: 72, borderRadius: 14, background: "var(--bg-2)",
            display: "grid", placeItems: "center",
            border: "1px solid var(--border)",
          }}>
            <Icon name="subsystem" size={32} className="muted" />
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600, letterSpacing: "-0.01em" }}>Internal Collections is not configured</div>
            <div className="muted text-sm" style={{ marginTop: 4, lineHeight: 1.55 }}>
              Activate to enable semantic search across <span className="mono">agents</span>,{" "}
              <span className="mono">graphs</span>, <span className="mono">collections</span>, and{" "}
              <span className="mono">tools</span>. The four <span className="mono">/v1/{`{kind}`}/search</span>{" "}
              routes return 503 until this subsystem is active.
            </div>
          </div>
          <Btn kind="primary" icon="settings" onClick={() => setConfigureOpen(true)}>Configure</Btn>
        </div>
      </div>

      {configureOpen && (
        <ConfigureModal
          onClose={() => setConfigureOpen(false)}
          onSaved={() => {
            setConfigureOpen(false);
            pushToast({ kind: "success", title: "Subsystem configured", detail: "Bootstrap required before search routes return results." });
            onRefresh();
          }}
          pushToast={pushToast}
        />
      )}
    </>
  );
}

// ============================================================================
// State 2 — Configured (200, activated_at null)
// ============================================================================

function ConfiguredCard({ config, onRefresh, pushToast }) {
  const { apiFetch, useMutation } = window.primerApi;
  const [bootstrapResult, setBootstrapResult] = React.useState(null);
  const [updateOpen, setUpdateOpen] = React.useState(false);

  const bootstrap = useMutation(
    () => apiFetch("POST", "/internal_collections/bootstrap"),
    {
      invalidates: [IC_CACHE_CONFIG],
      onSuccess: (resp) => {
        setBootstrapResult(resp);
        pushToast({ kind: "success", title: "Bootstrap complete", detail: "Subsystem is now active." });
        onRefresh();
      },
      onError: _icToastErr(pushToast, "Bootstrap failed"),
    }
  );

  return (
    <>
      <Banner
        kind="warning"
        title="Subsystem configured — bootstrap required"
        detail="Bootstrap ingests existing entities (agents / graphs / collections / tools) into internal vector collections. First run can take 30–60s; the CDC worker keeps them in sync afterwards."
      />

      <div className="panel">
        <div className="panel-h">
          <Icon name="settings" size={13} className="muted" />
          <span>Configuration</span>
        </div>
        <div className="panel-body">
          <ConfigKV config={config} />
          <div className="mt-3" style={{ display: "flex", gap: 6 }}>
            <Btn kind="primary" icon="play" onClick={() => bootstrap.mutate()} disabled={bootstrap.loading}>
              {bootstrap.loading ? "Bootstrapping…" : "Bootstrap now"}
            </Btn>
            <Btn kind="ghost" icon="settings" onClick={() => setUpdateOpen(true)}>Update config</Btn>
            <DeactivateButton onRefresh={onRefresh} pushToast={pushToast} />
          </div>
          {bootstrapResult && <BootstrapResultPanel result={bootstrapResult} />}
        </div>
      </div>

      {updateOpen && (
        <ConfigureModal
          existing={config}
          onClose={() => setUpdateOpen(false)}
          onSaved={() => { setUpdateOpen(false); pushToast({ kind: "info", title: "Config updated" }); onRefresh(); }}
          pushToast={pushToast}
        />
      )}
    </>
  );
}

// ============================================================================
// State 3 — Active (200, activated_at set)
// ============================================================================

function ActiveCard({ config, onRefresh, pushToast }) {
  const { apiFetch, useMutation, useRouter } = window.primerApi;
  const { navigate } = useRouter();
  const [bootstrapResult, setBootstrapResult] = React.useState(null);
  const [updateOpen, setUpdateOpen] = React.useState(false);

  const bootstrap = useMutation(
    () => apiFetch("POST", "/internal_collections/bootstrap"),
    {
      invalidates: [IC_CACHE_CONFIG],
      onSuccess: (resp) => {
        setBootstrapResult(resp);
        pushToast({ kind: "success", title: "Re-bootstrap complete" });
        onRefresh();
      },
      onError: _icToastErr(pushToast, "Re-bootstrap failed"),
    }
  );

  const bootstrapAgo = config.activated_at
    ? relativeTime((Date.now() - new Date(config.activated_at).getTime()) / 1000)
    : "—";

  return (
    <>
      <div className="panel" style={{
        background: "linear-gradient(90deg, var(--green-dim) 0%, var(--bg-1) 60%)",
        borderColor: "oklch(0.75 0.15 145 / 0.3)",
      }}>
        <div className="panel-body" style={{ display: "flex", alignItems: "center", gap: 16, padding: "18px 22px" }}>
          <div style={{
            width: 56, height: 56, borderRadius: 12, background: "var(--green)",
            display: "grid", placeItems: "center",
            boxShadow: "0 0 0 4px var(--green-dim)",
          }}>
            <Icon name="check" size={28} style={{ color: "var(--accent-fg)" }} />
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 600, letterSpacing: "-0.01em" }}>Internal Collections is active</div>
            <div className="muted text-sm" style={{ marginTop: 2 }}>
              Last bootstrap: {bootstrapAgo}
              {" · "}<span className="mono">{config.embedding_provider_id}</span>/<span className="mono">{config.embedding_model}</span>
            </div>
          </div>
          <Btn kind="ghost" icon="search" onClick={() => navigate("/knowledge/search")}>Run a search</Btn>
        </div>
      </div>

      <div className="panel">
        <div className="panel-h">
          <Icon name="settings" size={13} className="muted" />
          <span>Configuration</span>
        </div>
        <div className="panel-body">
          <ConfigKV config={config} />
          <div className="mt-3" style={{ display: "flex", gap: 6 }}>
            <Btn kind="primary" icon="refresh" onClick={() => bootstrap.mutate()} disabled={bootstrap.loading}>
              {bootstrap.loading ? "Re-bootstrapping…" : "Re-bootstrap"}
            </Btn>
            <Btn kind="ghost" icon="settings" onClick={() => setUpdateOpen(true)}>Update config</Btn>
            <DeactivateButton onRefresh={onRefresh} pushToast={pushToast} />
          </div>
          {bootstrapResult && <BootstrapResultPanel result={bootstrapResult} />}
        </div>
      </div>

      <Banner
        kind="info"
        icon="info"
        title="Eventual consistency"
        detail="The CDC worker syncs new agents/graphs/collections/tools asynchronously. Newly-created entities may not appear in search results for ~30s."
      />

      {updateOpen && (
        <ConfigureModal
          existing={config}
          onClose={() => setUpdateOpen(false)}
          onSaved={() => { setUpdateOpen(false); pushToast({ kind: "info", title: "Config updated" }); onRefresh(); }}
          pushToast={pushToast}
        />
      )}
    </>
  );
}

// ============================================================================
// Deactivate button (confirm modal → DELETE /internal_collections/config)
// ============================================================================

function DeactivateButton({ onRefresh, pushToast }) {
  const { apiFetch, useMutation } = window.primerApi;
  const [open, setOpen] = React.useState(false);
  const deactivate = useMutation(
    () => apiFetch("DELETE", "/internal_collections/config"),
    {
      invalidates: [IC_CACHE_CONFIG],
      onSuccess: () => {
        pushToast({ kind: "warning", title: "Subsystem deactivated", detail: "Search routes will return 503 until reconfigured." });
        onRefresh();
      },
      onError: _icToastErr(pushToast, "Deactivate failed"),
    }
  );
  return (
    <>
      <Btn kind="danger" icon="trash" onClick={() => setOpen(true)} disabled={deactivate.loading}>
        {deactivate.loading ? "Deactivating…" : "Deactivate"}
      </Btn>
      {open && (
        <Modal
          title="Deactivate Internal Collections?"
          danger
          onClose={() => setOpen(false)}
          footer={
            <>
              <Btn kind="ghost" onClick={() => setOpen(false)}>Cancel</Btn>
              <Btn kind="danger" icon="trash" onClick={async () => { setOpen(false); try { await deactivate.mutate(); } catch (_e) { /* toast already raised */ } }}>Deactivate</Btn>
            </>
          }
        >
          <ul>
            <li>Removes the <span className="mono">InternalCollectionsConfig</span> row.</li>
            <li>All four <span className="mono">/v1/{`{kind}`}/search</span> routes immediately return 503 <span className="mono">/errors/subsystem-inactive</span>.</li>
            <li>The CDC worker stops; new entities will <strong>not</strong> be indexed.</li>
            <li>Indexed data is NOT deleted by this call — it remains in the vector store; re-activation reuses it (cheap re-bootstrap).</li>
          </ul>
        </Modal>
      )}
    </>
  );
}

// ============================================================================
// Configure / update modal (PUT /internal_collections/config)
// ============================================================================

function ConfigureModal({ existing, onClose, onSaved, pushToast }) {
  const { apiFetch, useResource, useMutation } = window.primerApi;
  const embedProviders = useResource(
    IC_CACHE_EMBED,
    (signal) => apiFetch("GET", "/embedding_providers?limit=200", null, { signal }),
    {}
  );
  const rerankers = useResource(
    IC_CACHE_RERANK,
    (signal) => apiFetch("GET", "/cross_encoder_providers?limit=200", null, { signal }),
    {}
  );
  const ssps = useResource(
    IC_CACHE_SSP,
    (signal) => apiFetch("GET", "/ssp?limit=200", null, { signal }),
    {}
  );

  const [searchProviderId, setSearchProviderId] = React.useState(existing?.search_provider_id || "");
  const [providerId, setProviderId] = React.useState(existing?.embedding_provider_id || "");
  const [model, setModel] = React.useState(existing?.embedding_model || "");
  const [useMmr, setUseMmr] = React.useState(!!existing?.mmr);
  const [mmrLambda, setMmrLambda] = React.useState(existing?.mmr?.lambda ?? 0.5);
  const [useReranker, setUseReranker] = React.useState(!!existing?.cross_encoder);
  const [rerankerProviderId, setRerankerProviderId] = React.useState(existing?.cross_encoder?.provider_id || "");
  const [rerankerModel, setRerankerModel] = React.useState(existing?.cross_encoder?.model || "");
  const [fieldErrors, setFieldErrors] = React.useState({});

  // Auto-select first available SSP when the list resolves.
  React.useEffect(() => {
    if (!searchProviderId && ssps.data?.items?.length) {
      setSearchProviderId(ssps.data.items[0].id);
    }
  }, [ssps.data, searchProviderId]);

  // Auto-select first embedding provider when the list resolves.
  React.useEffect(() => {
    if (!providerId && embedProviders.data?.items?.length) {
      setProviderId(embedProviders.data.items[0].id);
    }
  }, [embedProviders.data, providerId]);

  const selectedProvider = (embedProviders.data?.items ?? []).find((p) => p.id === providerId);
  const modelOptions = selectedProvider?.models ?? [];
  React.useEffect(() => {
    if (modelOptions.length > 0 && !modelOptions.some((m) => m.name === model)) {
      setModel(modelOptions[0].name);
    }
  }, [modelOptions]);  // eslint-disable-line react-hooks/exhaustive-deps

  React.useEffect(() => {
    if (useReranker && !rerankerProviderId && rerankers.data?.items?.length) {
      setRerankerProviderId(rerankers.data.items[0].id);
    }
  }, [useReranker, rerankers.data, rerankerProviderId]);
  const selectedReranker = (rerankers.data?.items ?? []).find((p) => p.id === rerankerProviderId);
  const rerankerModelOptions = selectedReranker?.models ?? [];
  React.useEffect(() => {
    if (useReranker && rerankerModelOptions.length > 0 && !rerankerModelOptions.some((m) => m.name === rerankerModel)) {
      setRerankerModel(rerankerModelOptions[0].name);
    }
  }, [useReranker, rerankerModelOptions]);  // eslint-disable-line react-hooks/exhaustive-deps

  const save = useMutation(
    (body) => apiFetch("PUT", "/internal_collections/config", body),
    {
      invalidates: [IC_CACHE_CONFIG],
      onSuccess: () => onSaved(),
      onError: (err) => {
        if (err?.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) next[(fe.loc || []).join(".")] = fe.msg;
          setFieldErrors(next);
        } else {
          _icToastErr(pushToast, "Save failed")(err);
        }
      },
    }
  );

  const submit = async () => {
    setFieldErrors({});
    const body = {
      search_provider_id: searchProviderId,
      embedding_provider_id: providerId,
      embedding_model: model,
    };
    if (useReranker && rerankerProviderId) {
      body.cross_encoder = { provider_id: rerankerProviderId, model: rerankerModel };
    }
    if (useMmr) {
      body.mmr = { lambda: Number(mmrLambda) };
    }
    try { await save.mutate(body); } catch (_e) { /* onError handled */ }
  };

  const noSSPs = !ssps.loading && (ssps.data?.items ?? []).length === 0;
  const noEmbed = !embedProviders.loading && (embedProviders.data?.items ?? []).length === 0;
  const disabled = !searchProviderId || !providerId || !model || save.loading;

  return (
    <Modal
      title={existing ? "Update Internal Collections" : "Configure Internal Collections"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="check" onClick={submit} disabled={disabled}>
            {save.loading ? "Saving…" : "Save"}
          </Btn>
        </>
      }
    >
      {noSSPs && (
        <Banner
          kind="warning"
          title="No Semantic Search providers configured"
          detail="The internal collections subsystem needs an SSP to back its four reserved collections. Create one at /ssp first."
        />
      )}
      <div className="field">
        <label className="field-label">Semantic Search provider <span className="hint">required — backs the 4 reserved internal collections</span></label>
        <select className="select mono" value={searchProviderId} onChange={(e) => setSearchProviderId(e.target.value)} style={{ width: "100%" }}>
          <option value="">-- pick a provider --</option>
          {(ssps.data?.items ?? []).map((p) => <option key={p.id} value={p.id}>{p.id}{p.provider ? ` · ${p.provider}` : ""}</option>)}
        </select>
        {fieldErrors["body.search_provider_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.search_provider_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Embedding provider</label>
        <select className="select" value={providerId} onChange={(e) => setProviderId(e.target.value)} style={{ width: "100%" }}>
          <option value="">-- pick a provider --</option>
          {(embedProviders.data?.items ?? []).map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        {noEmbed && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No embedding providers configured. Create one at <span className="mono">/providers/embedding</span> first.
          </div>
        )}
        {fieldErrors["body.embedding_provider_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.embedding_provider_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Embedding model</label>
        <select className="select" value={model} onChange={(e) => setModel(e.target.value)} style={{ width: "100%" }}>
          <option value="">-- pick a model --</option>
          {modelOptions.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
        </select>
        {fieldErrors["body.embedding_model"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.embedding_model"]}</div>}
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input id="ic-mmr" type="checkbox" checked={useMmr} onChange={(e) => setUseMmr(e.target.checked)} />
        <label htmlFor="ic-mmr" className="field-label" style={{ margin: 0, cursor: "pointer" }}>MMR diversification</label>
      </div>
      {useMmr && (
        <div className="field" style={{ paddingLeft: 22 }}>
          <label className="field-label">lambda (0–1)</label>
          <input className="input" type="number" min={0} max={1} step={0.05} value={mmrLambda} onChange={(e) => setMmrLambda(e.target.value)} style={{ width: 100 }} />
        </div>
      )}
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input id="ic-cer" type="checkbox" checked={useReranker} onChange={(e) => setUseReranker(e.target.checked)} />
        <label htmlFor="ic-cer" className="field-label" style={{ margin: 0, cursor: "pointer" }}>Cross-encoder reranker</label>
      </div>
      {useReranker && (
        <>
          <div className="field" style={{ paddingLeft: 22 }}>
            <label className="field-label">Reranker provider</label>
            <select className="select" value={rerankerProviderId} onChange={(e) => setRerankerProviderId(e.target.value)} style={{ width: "100%" }}>
              <option value="">-- pick a reranker --</option>
              {(rerankers.data?.items ?? []).map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
            </select>
            {(rerankers.data?.items ?? []).length === 0 && !rerankers.loading && (
              <div className="field-help" style={{ color: "var(--amber)" }}>
                No cross-encoder providers configured. Create one at <span className="mono">/providers/cross_encoder</span> first.
              </div>
            )}
          </div>
          <div className="field" style={{ paddingLeft: 22 }}>
            <label className="field-label">Reranker model</label>
            <select className="select" value={rerankerModel} onChange={(e) => setRerankerModel(e.target.value)} style={{ width: "100%" }}>
              <option value="">-- pick a model --</option>
              {rerankerModelOptions.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
            </select>
          </div>
        </>
      )}
    </Modal>
  );
}

// ============================================================================
// Helpers
// ============================================================================

function ConfigKV({ config }) {
  return (
    <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
      <dt>search provider</dt><dd className="mono">{config.search_provider_id || <span className="muted">—</span>}</dd>
      <dt>embedding provider</dt><dd className="mono">{config.embedding_provider_id}</dd>
      <dt>embedding model</dt><dd className="mono">{config.embedding_model}</dd>
      <dt>MMR</dt><dd>{config.mmr ? <span className="mono">λ={config.mmr.lambda}</span> : <span className="muted">disabled</span>}</dd>
      <dt>cross-encoder</dt><dd>{config.cross_encoder ? <span className="mono">{config.cross_encoder.provider_id} · {config.cross_encoder.model}</span> : <span className="muted">disabled</span>}</dd>
      <dt>activated at</dt><dd>{config.activated_at || <span className="muted">never (not yet bootstrapped)</span>}</dd>
    </dl>
  );
}

function BootstrapResultPanel({ result }) {
  const highlight = window.primerVendor?.highlightJson;
  const text = JSON.stringify(result, null, 2);
  return (
    <div className="mt-3 panel" style={{ background: "var(--bg-2)" }}>
      <div className="panel-h">
        <Icon name="check-circle" size={13} style={{ color: "var(--green)" }} />
        <span>Bootstrap result</span>
      </div>
      <div className="panel-body" style={{ padding: 12 }}>
        {highlight
          ? <div className="code-block" dangerouslySetInnerHTML={{ __html: highlight(text) }} />
          : <pre className="code-block mono" style={{ margin: 0 }}>{text}</pre>}
      </div>
    </div>
  );
}

window.InternalCollectionsPage = InternalCollectionsPage;
