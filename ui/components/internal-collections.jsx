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
//   DELETE /internal_collections/config       (deactivate; drops the 4 reserved IC collections)
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
const IC_CACHE_BOOTSTRAP = "ic:bootstrap-status";

// Six steps in order — keeps the UI's per-phase rendering in sync with
// the backend's phase enum. Used to compute the global progress bar
// (current_phase_index + within_phase_fraction) / total_phases.
const IC_BOOTSTRAP_PHASES = [
  { id: "drain_queue", label: "Draining CDC queue" },
  { id: "materialise_collections", label: "Materialising collections" },
  { id: "ingest_agents", label: "Ingesting agents" },
  { id: "ingest_graphs", label: "Ingesting graphs" },
  { id: "ingest_collections", label: "Ingesting collections" },
  { id: "ingest_tools", label: "Ingesting tools" },
  { id: "finalize", label: "Finalising" },
];

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

// Poll the bootstrap-status row. Fast cadence (1s) while a bootstrap
// is actively running so the progress bar feels alive; slow cadence
// (5s) otherwise so the page doesn't churn the network when idle.
//
// Returns the same shape as useResource: {data, error, loading, refetch}.
function _useBootstrapStatus() {
  const { useResource } = window.primerApi;
  const [pollMs, setPollMs] = React.useState(5000);
  const res = useResource(
    IC_CACHE_BOOTSTRAP,
    (signal) => window.primerApi.apiFetch(
      "GET", "/internal_collections/bootstrap/status", null, { signal },
    ),
    { pollMs },
  );
  React.useEffect(() => {
    const next = res.data?.status === "running" ? 1000 : 5000;
    if (next !== pollMs) setPollMs(next);
  }, [res.data?.status, pollMs]);
  return res;
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
  const [updateOpen, setUpdateOpen] = React.useState(false);
  const bootstrapStatus = _useBootstrapStatus();

  const bootstrap = useMutation(
    () => apiFetch("POST", "/internal_collections/bootstrap"),
    {
      invalidates: [IC_CACHE_CONFIG, IC_CACHE_BOOTSTRAP],
      onSuccess: () => {
        pushToast({ kind: "info", title: "Bootstrap started", detail: "Running in the background — leave this page if you like." });
        bootstrapStatus.refetch();
      },
      onError: (err) => {
        if (err?.status === 409) {
          // Already running — just sync the status and let the UI render.
          bootstrapStatus.refetch();
          return;
        }
        _icToastErr(pushToast, "Bootstrap failed")(err);
      },
    }
  );

  // When a running bootstrap finishes, sync the config (which now has
  // activated_at set). The page-level useResource flips us from
  // ConfiguredCard to ActiveCard on the next render.
  const prevStatusRef = React.useRef(null);
  React.useEffect(() => {
    const prev = prevStatusRef.current;
    const curr = bootstrapStatus.data?.status;
    if (prev === "running" && curr === "succeeded") {
      pushToast({ kind: "success", title: "Bootstrap complete", detail: "Subsystem is now active." });
      onRefresh();
    } else if (prev === "running" && curr === "failed") {
      pushToast({
        kind: "error",
        title: "Bootstrap failed",
        detail: bootstrapStatus.data?.error || "See server logs for details.",
      });
    }
    prevStatusRef.current = curr;
  }, [bootstrapStatus.data?.status]);

  const status = bootstrapStatus.data;
  const isRunning = status?.status === "running";

  return (
    <>
      {!isRunning && (
        <Banner
          kind="warning"
          title="Subsystem configured — bootstrap required"
          detail="Bootstrap ingests existing entities (agents / graphs / collections / tools) into internal vector collections. First run can take 30–60s; the CDC worker keeps them in sync afterwards."
        />
      )}

      {status?.status === "failed" && (
        <Banner
          kind="error"
          title="Last bootstrap failed"
          detail={status.error || "See server logs for details."}
          actions={<Btn size="sm" icon="refresh" onClick={() => bootstrap.mutate()}>Retry bootstrap</Btn>}
        />
      )}

      <div className="panel">
        <div className="panel-h">
          <Icon name="settings" size={13} className="muted" />
          <span>Configuration</span>
        </div>
        <div className="panel-body">
          <ConfigKV config={config} />
          <div className="mt-3" style={{ display: "flex", gap: 6 }}>
            <Btn kind="primary" icon="play" onClick={() => bootstrap.mutate()} disabled={isRunning || bootstrap.loading}>
              {isRunning ? "Bootstrapping…" : "Bootstrap now"}
            </Btn>
            <Btn kind="ghost" icon="settings" onClick={() => setUpdateOpen(true)} disabled={isRunning}>Update config</Btn>
            <DeactivateButton onRefresh={onRefresh} pushToast={pushToast} disabled={isRunning} />
          </div>
          {isRunning && <BootstrapProgressPanel status={status} />}
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
  const [updateOpen, setUpdateOpen] = React.useState(false);
  const bootstrapStatus = _useBootstrapStatus();

  const bootstrap = useMutation(
    () => apiFetch("POST", "/internal_collections/bootstrap"),
    {
      invalidates: [IC_CACHE_CONFIG, IC_CACHE_BOOTSTRAP],
      onSuccess: () => {
        pushToast({ kind: "info", title: "Re-bootstrap started", detail: "Running in the background — leave this page if you like." });
        bootstrapStatus.refetch();
      },
      onError: (err) => {
        if (err?.status === 409) {
          bootstrapStatus.refetch();
          return;
        }
        _icToastErr(pushToast, "Re-bootstrap failed")(err);
      },
    }
  );

  const prevStatusRef = React.useRef(null);
  React.useEffect(() => {
    const prev = prevStatusRef.current;
    const curr = bootstrapStatus.data?.status;
    if (prev === "running" && curr === "succeeded") {
      pushToast({ kind: "success", title: "Re-bootstrap complete" });
      onRefresh();
    } else if (prev === "running" && curr === "failed") {
      pushToast({
        kind: "error",
        title: "Re-bootstrap failed",
        detail: bootstrapStatus.data?.error || "See server logs for details.",
      });
    }
    prevStatusRef.current = curr;
  }, [bootstrapStatus.data?.status]);

  const status = bootstrapStatus.data;
  const isRunning = status?.status === "running";

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
            <Btn kind="primary" icon="refresh" onClick={() => bootstrap.mutate()} disabled={isRunning || bootstrap.loading}>
              {isRunning ? "Re-bootstrapping…" : "Re-bootstrap"}
            </Btn>
            <Btn kind="ghost" icon="settings" onClick={() => setUpdateOpen(true)} disabled={isRunning}>Update config</Btn>
            <DeactivateButton onRefresh={onRefresh} pushToast={pushToast} disabled={isRunning} />
          </div>
          {isRunning && <BootstrapProgressPanel status={status} />}
        </div>
      </div>

      {status?.status === "failed" && (
        <Banner
          kind="error"
          title="Last re-bootstrap failed"
          detail={status.error || "See server logs for details."}
          actions={<Btn size="sm" icon="refresh" onClick={() => bootstrap.mutate()}>Retry</Btn>}
        />
      )}

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

function DeactivateButton({ onRefresh, pushToast, disabled }) {
  const { apiFetch, useMutation } = window.primerApi;
  const [open, setOpen] = React.useState(false);
  const deactivate = useMutation(
    () => apiFetch("DELETE", "/internal_collections/config"),
    {
      invalidates: [IC_CACHE_CONFIG],
      onSuccess: () => {
        pushToast({ kind: "warning", title: "Subsystem deactivated", detail: "Config row removed and reserved collections dropped. Re-PUT + bootstrap to rebuild." });
        onRefresh();
      },
      onError: _icToastErr(pushToast, "Deactivate failed"),
    }
  );
  return (
    <>
      <Btn kind="danger" icon="trash" onClick={() => setOpen(true)} disabled={disabled || deactivate.loading}>
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
            <li><strong>Drops</strong> the four reserved collections (<span className="mono">_internal_agents</span>, <span className="mono">_internal_graphs</span>, <span className="mono">_internal_collections</span>, <span className="mono">_internal_tools</span>) and all their embeddings from the backing SSP — this is what lets you switch embedding models cleanly. Re-PUT + re-bootstrap rebuilds from scratch.</li>
            <li>Custom (non-IC) collections in the same SSP are <strong>not</strong> touched.</li>
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
  // Vector-space-defining fields are frozen once activated_at is set.
  // Changing them post-bootstrap would mix vectors from incompatible
  // spaces; the operator must deactivate (which drops the reserved
  // collections and removes the config row) before swapping.
  // cross_encoder and mmr are reranking concerns and remain editable.
  const vectorSpaceLocked = !!existing?.activated_at;
  const lockedHint = "Locked — deactivate the subsystem to change this. Existing embeddings are tied to this provider/model and can't be searched with a different one.";

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
      {vectorSpaceLocked && (
        <Banner
          kind="info"
          icon="info"
          title="Vector-space fields are locked while active"
          detail="The SSP, embedding provider, and embedding model can't change while the subsystem is active — existing embeddings are tied to them. Deactivate to swap: the config row is removed and the four reserved IC collections are dropped, so you'll re-bootstrap from scratch."
        />
      )}
      <div className="field">
        <label className="field-label">Semantic Search provider <span className="hint">required — backs the 4 reserved internal collections</span></label>
        <select className="select mono" value={searchProviderId} onChange={(e) => setSearchProviderId(e.target.value)} disabled={vectorSpaceLocked} style={{ width: "100%" }}>
          <option value="">-- pick a provider --</option>
          {(ssps.data?.items ?? []).map((p) => <option key={p.id} value={p.id}>{p.id}{p.provider ? ` · ${p.provider}` : ""}</option>)}
        </select>
        {vectorSpaceLocked && <div className="field-help muted">{lockedHint}</div>}
        {fieldErrors["body.search_provider_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.search_provider_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Embedding provider</label>
        <select className="select" value={providerId} onChange={(e) => setProviderId(e.target.value)} disabled={vectorSpaceLocked} style={{ width: "100%" }}>
          <option value="">-- pick a provider --</option>
          {(embedProviders.data?.items ?? []).map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
        </select>
        {noEmbed && (
          <div className="field-help" style={{ color: "var(--amber)" }}>
            No embedding providers configured. Create one at <span className="mono">/providers/embedding</span> first.
          </div>
        )}
        {vectorSpaceLocked && !noEmbed && <div className="field-help muted">{lockedHint}</div>}
        {fieldErrors["body.embedding_provider_id"] && <div className="field-help" style={{ color: "var(--red)" }}>{fieldErrors["body.embedding_provider_id"]}</div>}
      </div>
      <div className="field">
        <label className="field-label">Embedding model</label>
        <select className="select" value={model} onChange={(e) => setModel(e.target.value)} disabled={vectorSpaceLocked} style={{ width: "100%" }}>
          <option value="">-- pick a model --</option>
          {modelOptions.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
        </select>
        {vectorSpaceLocked && <div className="field-help muted">{lockedHint}</div>}
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

function BootstrapProgressPanel({ status }) {
  // Compute a single global progress fraction by reading where we are
  // in the phase sequence + how far into the current phase. Each phase
  // contributes 1/N to the bar; within-phase progress prorates the
  // current phase's contribution.
  const phaseIndex = Math.max(0, IC_BOOTSTRAP_PHASES.findIndex(p => p.id === status.phase));
  const within = status.phase_total && status.phase_total > 0
    ? Math.min(1, status.phase_done / status.phase_total)
    : 0;
  const total = IC_BOOTSTRAP_PHASES.length;
  const fraction = (phaseIndex + within) / total;
  const percent = Math.max(0, Math.min(100, Math.round(fraction * 100)));

  const currentLabel = IC_BOOTSTRAP_PHASES[phaseIndex]?.label || "Working…";
  const counts = status.counts || {};
  const elapsedS = status.started_at
    ? Math.max(0, Math.round((Date.now() - new Date(status.started_at).getTime()) / 1000))
    : 0;

  return (
    <div className="mt-3 panel" style={{ background: "var(--bg-2)" }}>
      <div className="panel-h">
        <Icon name="refresh" size={13} className="muted" style={{ animation: "ic-bootstrap-spin 1s linear infinite" }} />
        <span>Bootstrap in progress</span>
        <span className="muted text-sm" style={{ marginLeft: "auto", fontVariantNumeric: "tabular-nums" }}>
          {percent}% · {elapsedS}s
        </span>
      </div>
      <div className="panel-body" style={{ padding: 12 }}>
        {/* progress bar */}
        <div style={{
          height: 6,
          background: "var(--bg)",
          border: "1px solid var(--border)",
          borderRadius: 3,
          overflow: "hidden",
        }}>
          <div style={{
            width: `${percent}%`,
            height: "100%",
            background: "var(--accent)",
            transition: "width 0.3s ease-out",
          }} />
        </div>

        {/* current phase + per-phase count */}
        <div style={{ marginTop: 10, display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
          <span style={{ fontWeight: 500 }}>{currentLabel}</span>
          {status.phase_total != null && status.phase_total > 0 && (
            <span className="mono muted text-sm" style={{ fontVariantNumeric: "tabular-nums" }}>
              {status.phase_done} / {status.phase_total}
            </span>
          )}
          {status.phase_total == null && status.phase_done > 0 && (
            <span className="mono muted text-sm" style={{ fontVariantNumeric: "tabular-nums" }}>
              {status.phase_done}
            </span>
          )}
        </div>

        {/* running totals */}
        <div className="mt-3" style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8 }}>
          {[
            { key: "agents", label: "Agents" },
            { key: "graphs", label: "Graphs" },
            { key: "collections", label: "Collections" },
            { key: "tools", label: "Tools" },
          ].map(({ key, label }) => (
            <div key={key} style={{
              padding: "6px 8px",
              background: "var(--bg)",
              border: "1px solid var(--border)",
              borderRadius: 4,
            }}>
              <div className="muted text-sm" style={{ fontSize: 11 }}>{label}</div>
              <div className="mono" style={{ fontSize: 15, fontVariantNumeric: "tabular-nums" }}>
                {counts[key] ?? 0}
              </div>
            </div>
          ))}
        </div>

        <div className="muted text-sm mt-3" style={{ fontSize: 11 }}>
          Bootstrap runs in a background task on the server. You can navigate
          away from this page — progress will resume here when you return.
        </div>
      </div>
    </div>
  );
}

window.InternalCollectionsPage = InternalCollectionsPage;
