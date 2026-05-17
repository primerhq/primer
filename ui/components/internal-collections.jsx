/* global React, Icon, Btn, Modal, Banner, relativeTime */

const { apiFetch, useResource, useMutation, useRouter, useToast } = window.matrixApi;

// 3-state wizard:
//   - Inactive: GET /v1/internal_collections/config returns 404
//   - Configured: config exists but `activated_at` is null → bootstrap required
//   - Active: config exists AND `activated_at` is set
//
// Server has the ground truth via `activated_at` — cleaner than the
// localStorage flag the original spec suggested.
async function _fetchIcConfig(signal) {
  try {
    return await apiFetch("GET", "/internal_collections/config", null, { signal });
  } catch (err) {
    if (err && err.status === 404) return null;
    throw err;
  }
}

function InternalCollectionsPage() {
  const { push: pushToast } = useToast();
  const ic = useResource("sidebar:ic-config", _fetchIcConfig, { pollMs: 30000 });

  const state = ic.data == null
    ? "inactive"
    : ic.data.activated_at
      ? "active"
      : "configured";

  return (
    <div className="col" style={{ gap: 14 }}>
      <IcHeader state={state} />

      {state === "inactive" && <InactiveCard onRefresh={ic.refetch} pushToast={pushToast} />}
      {state === "configured" && <ConfiguredCard config={ic.data} onRefresh={ic.refetch} pushToast={pushToast} />}
      {state === "active" && <ActiveCard config={ic.data} onRefresh={ic.refetch} pushToast={pushToast} />}

      {ic.error && (
        <Banner kind="error" title={ic.error.title || "Couldn't load subsystem config"} detail={ic.error.detail || ic.error.message}
          actions={<Btn size="sm" icon="refresh" onClick={ic.refetch}>Retry</Btn>} />
      )}
    </div>
  );
}

function IcHeader({ state }) {
  const pillCls = state === "active" ? "pill-ended" : state === "configured" ? "pill-paused" : "pill-cancelled";
  return (
    <div className="page-header" style={{ marginBottom: 0 }}>
      <div>
        <div className="crumb">
          <span>Subsystems</span><span className="sep">/</span><span style={{ color: "var(--text)" }}>Internal Collections</span>
        </div>
        <h1 className="page-title">Internal Collections</h1>
        <div className="page-sub">Powers semantic search across agents, graphs, collections, and tools.</div>
      </div>
      <div className="page-actions">
        <span className={`pill ${pillCls}`}>
          <span className="dot"></span>{state}
        </span>
      </div>
    </div>
  );
}

// ============================================================================
// State 1 — Inactive
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
          onSaved={() => { setConfigureOpen(false); pushToast({ kind: "success", title: "Subsystem configured", detail: "Bootstrap required before search routes return results." }); onRefresh(); }}
        />
      )}
    </>
  );
}

// ============================================================================
// State 2 — Configured (not yet bootstrapped)
// ============================================================================

function ConfiguredCard({ config, onRefresh, pushToast }) {
  const [bootstrapResult, setBootstrapResult] = React.useState(null);
  const [updateOpen, setUpdateOpen] = React.useState(false);

  const bootstrap = useMutation(
    () => apiFetch("POST", "/internal_collections/bootstrap"),
    {
      invalidates: ["sidebar:ic-config"],
      onSuccess: (resp) => {
        setBootstrapResult(resp);
        pushToast({ kind: "success", title: "Bootstrap complete", detail: "Subsystem is now active." });
        onRefresh();
      },
      onError: (err) => pushToast({ kind: "error", title: "Bootstrap failed", detail: err.detail || err.message, requestId: err.requestId }),
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
        />
      )}
    </>
  );
}

// ============================================================================
// State 3 — Active
// ============================================================================

function ActiveCard({ config, onRefresh, pushToast }) {
  const [bootstrapResult, setBootstrapResult] = React.useState(null);
  const [updateOpen, setUpdateOpen] = React.useState(false);
  const { navigate } = useRouter();

  const bootstrap = useMutation(
    () => apiFetch("POST", "/internal_collections/bootstrap"),
    {
      invalidates: ["sidebar:ic-config"],
      onSuccess: (resp) => { setBootstrapResult(resp); pushToast({ kind: "success", title: "Re-bootstrap complete" }); onRefresh(); },
      onError: (err) => pushToast({ kind: "error", title: "Re-bootstrap failed", detail: err.detail || err.message, requestId: err.requestId }),
    }
  );

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
              Last bootstrap: {config.activated_at ? relativeTime((Date.now() - new Date(config.activated_at).getTime()) / 1000) : "—"}
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
        />
      )}
    </>
  );
}

// ============================================================================
// Deactivate button
// ============================================================================

function DeactivateButton({ onRefresh, pushToast }) {
  const [open, setOpen] = React.useState(false);
  const deactivate = useMutation(
    () => apiFetch("DELETE", "/internal_collections/config"),
    {
      invalidates: ["sidebar:ic-config"],
      onSuccess: () => { pushToast({ kind: "warning", title: "Subsystem deactivated", detail: "Search routes will return 503 until reconfigured." }); onRefresh(); },
      onError: (err) => pushToast({ kind: "error", title: "Deactivate failed", detail: err.detail || err.message, requestId: err.requestId }),
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
              <Btn kind="danger" icon="trash" onClick={async () => { setOpen(false); try { await deactivate.mutate(); } catch (_e) {} }}>Deactivate</Btn>
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
// Configure / update modal
// ============================================================================

function ConfigureModal({ existing, onClose, onSaved }) {
  const { push: pushToast } = useToast();
  const embedProviders = useResource("ic:embedding-providers",
    (s) => apiFetch("GET", "/embedding_providers?limit=200", null, { signal: s }), {});
  const rerankers = useResource("ic:rerank-providers",
    (s) => apiFetch("GET", "/cross_encoder_providers?limit=200", null, { signal: s }), {});

  const [providerId, setProviderId] = React.useState(existing?.embedding_provider_id || "");
  const [model, setModel] = React.useState(existing?.embedding_model || "");
  const [useMmr, setUseMmr] = React.useState(!!existing?.mmr);
  const [mmrLambda, setMmrLambda] = React.useState(existing?.mmr?.lambda ?? 0.5);
  const [useReranker, setUseReranker] = React.useState(!!existing?.cross_encoder);
  const [rerankerProviderId, setRerankerProviderId] = React.useState(existing?.cross_encoder?.provider_id || "");
  const [rerankerModel, setRerankerModel] = React.useState(existing?.cross_encoder?.model || "");
  const [fieldErrors, setFieldErrors] = React.useState({});

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
      invalidates: ["sidebar:ic-config"],
      onSuccess: () => onSaved(),
      onError: (err) => {
        if (err.status === 422 && Array.isArray(err.fieldErrors)) {
          const next = {};
          for (const fe of err.fieldErrors) next[(fe.loc || []).join(".")] = fe.msg;
          setFieldErrors(next);
        } else {
          pushToast({ kind: "error", title: err.title || "Save failed", detail: err.detail || err.message, requestId: err.requestId });
        }
      },
    }
  );

  const submit = async () => {
    setFieldErrors({});
    const body = {
      embedding_provider_id: providerId,
      embedding_model: model,
    };
    if (useReranker && rerankerProviderId) {
      body.cross_encoder = { provider_id: rerankerProviderId, model: rerankerModel };
    }
    if (useMmr) {
      body.mmr = { lambda: Number(mmrLambda) };
    }
    try { await save.mutate(body); } catch (_e) {}
  };

  return (
    <Modal
      title={existing ? "Update Internal Collections" : "Configure Internal Collections"}
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon="check" onClick={submit} disabled={!providerId || !model || save.loading}>
            {save.loading ? "Saving…" : "Save"}
          </Btn>
        </>
      }
    >
      <div className="field">
        <label className="field-label">Embedding provider</label>
        <select className="select" value={providerId} onChange={(e) => setProviderId(e.target.value)} style={{ width: "100%" }}>
          <option value="">-- pick a provider --</option>
          {(embedProviders.data?.items ?? []).map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        {(embedProviders.data?.items ?? []).length === 0 && !embedProviders.loading && (
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
    <dl className="kv" style={{ gridTemplateColumns: "160px 1fr" }}>
      <dt>embedding provider</dt><dd className="mono">{config.embedding_provider_id}</dd>
      <dt>embedding model</dt><dd className="mono">{config.embedding_model}</dd>
      <dt>MMR</dt><dd>{config.mmr ? `λ=${config.mmr.lambda}` : <span className="muted">disabled</span>}</dd>
      <dt>cross-encoder</dt><dd>{config.cross_encoder ? <span className="mono">{config.cross_encoder.provider_id} · {config.cross_encoder.model}</span> : <span className="muted">disabled</span>}</dd>
      <dt>activated at</dt><dd>{config.activated_at || <span className="muted">never (not yet bootstrapped)</span>}</dd>
    </dl>
  );
}

function BootstrapResultPanel({ result }) {
  return (
    <div className="mt-3 panel" style={{ background: "var(--bg-2)" }}>
      <div className="panel-h">
        <Icon name="check-circle" size={13} style={{ color: "var(--green)" }} />
        <span>Bootstrap result</span>
      </div>
      <div className="panel-body" style={{ padding: 12 }}>
        <div className="code-block" dangerouslySetInnerHTML={{ __html: window.matrixVendor.highlightJson(JSON.stringify(result, null, 2)) }} />
      </div>
    </div>
  );
}

window.InternalCollectionsPage = InternalCollectionsPage;
