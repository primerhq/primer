/* global React, Btn, Banner, Icon */
// First-run bootstrap wizard (S5 spec section 3).
//
// Two steps and nothing more: one LLM provider, one default model profile.
// Everything else about this install is configured conversationally with the
// operator afterwards.
//
// SetupWizardSteps is the EMBEDDABLE sequence: props only, no shell, no
// routes, no address-bar access, so the S8 studio can host it unchanged.
// SetupWizardGate is the console host that supplies the auth-shell chrome and
// owns the reload. It takes onDone only and renders NO children: AuthGate owns
// the branch that decides between this gate and the app, so nothing may wrap
// the console inside SetupWizardGate. SetupWaitingScreen is what non-admins see
// while an admin finishes setup.

const SETUP_PROVIDER_TYPES = [
  { id: "openchat", label: "OpenAI-compatible (chat completions)", needsUrl: true },
  { id: "openresponses", label: "OpenAI-compatible (responses)", needsUrl: true },
  { id: "ollama", label: "Ollama", needsUrl: true },
  { id: "anthropic", label: "Anthropic", needsUrl: false },
  { id: "gemini", label: "Gemini", needsUrl: false },
  { id: "openrouter", label: "OpenRouter", needsUrl: false },
];

function _setupDraftConfig(type, url, apiKey) {
  const spec = SETUP_PROVIDER_TYPES.find((t) => t.id === type);
  const config = {};
  if (spec && spec.needsUrl && url) config.url = url;
  if (apiKey) config.api_key = apiKey;
  return config;
}

function SetupWizardSteps({ onComplete, initialStep, initialModels }) {
  // initialStep exists for the docs harness, which captures each step as
  // its own image and cannot click through a wizard to reach the second
  // one. Nothing in the console passes it, so the wizard still opens
  // where a first-run operator expects: at the beginning.
  const [step, setStep] = React.useState(initialStep || 1);
  const [type, setType] = React.useState("openchat");
  const [url, setUrl] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [providerId, setProviderId] = React.useState("");
  // initialModels goes with initialStep: step 2's list is what step 1's
  // probe returned, so a capture that starts at step 2 has an empty
  // dropdown and documents nothing. Console callers pass neither.
  const [discovered, setDiscovered] = React.useState(initialModels || []);
  const [picked, setPicked] = React.useState(
    (initialModels && initialModels[0] && initialModels[0].name) || ""
  );
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState(null);

  const spec = SETUP_PROVIDER_TYPES.find((t) => t.id === type);

  // Step 1: a successful draft probe IS the proof that the provider works,
  // so the provider row is only persisted once the probe returns models.
  const submitProvider = async (e) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const config = _setupDraftConfig(type, url, apiKey);
      const probe = await window.primerApi.apiFetch(
        "POST", "/llm_providers/_discover_models",
        { provider: type, config }, {},
      );
      const models = (probe && probe.models) || [];
      if (!models.length) {
        setErr({ title: "No models returned", detail: "The provider answered but listed no models." });
        return;
      }
      const id = "llm-" + type;
      await window.primerApi.apiFetch(
        "POST", "/llm_providers",
        { id, provider: type, config, limits: { max_concurrency: 4 } }, {},
      );
      setProviderId(id);
      setDiscovered(models);
      setPicked(models[0].name);
      setStep(2);
    } catch (e2) {
      setErr({ title: "Could not reach that provider", detail: e2 && (e2.detail || e2.message) });
    } finally {
      setBusy(false);
    }
  };

  // Step 2: the SAME probe result is the model list; no second discovery call.
  const submitProfile = async (e) => {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      const model = discovered.find((m) => m.name === picked) || { name: picked };
      await window.primerApi.apiFetch(
        "POST", "/model_profiles",
        {
          id: providerId + "--" + model.name,
          description: "Default profile created by first-run setup.",
          provider_id: providerId,
          model_name: model.name,
          context_length: model.context_length || 32000,
        }, {},
      );
      await onComplete();
    } catch (e2) {
      setErr({ title: "Could not register that model", detail: e2 && (e2.detail || e2.message) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="setup-steps">
      <div className="setup-progress mono">Step {step} of 2</div>
      {err && (
        <div className="auth-banner">
          <div style={{ flex: 1 }}>
            <div className="title">{err.title}</div>
            {err.detail && <div className="detail">{String(err.detail)}</div>}
          </div>
        </div>
      )}
      {step === 1 && (
        <form className="auth-body" onSubmit={submitProvider} noValidate>
          <div className="auth-field">
            <label htmlFor="setup-type">Provider</label>
            <select
              id="setup-type"
              className="mono"
              value={type}
              onChange={(e) => setType(e.target.value)}
            >
              {SETUP_PROVIDER_TYPES.map((t) => (
                <option key={t.id} value={t.id}>{t.label}</option>
              ))}
            </select>
          </div>
          {spec && spec.needsUrl && (
            <div className="auth-field">
              <label htmlFor="setup-url">Base URL</label>
              <input
                id="setup-url"
                className="mono"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="http://localhost:11434"
                autoFocus
              />
            </div>
          )}
          <div className="auth-field">
            <label htmlFor="setup-key">API key</label>
            <input
              id="setup-key"
              className="mono"
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="leave blank for unauthenticated servers"
            />
          </div>
          <button
            type="submit"
            className="auth-submit touch-target"
            disabled={busy || (spec && spec.needsUrl && !url)}
          >
            {busy ? (<><span className="spinner" /><span>Checking…</span></>) : <span>Connect and list models</span>}
          </button>
        </form>
      )}
      {step === 2 && (
        <form className="auth-body" onSubmit={submitProfile} noValidate>
          <div className="auth-field">
            <label htmlFor="setup-model">Default model</label>
            <select
              id="setup-model"
              className="mono"
              value={picked}
              onChange={(e) => setPicked(e.target.value)}
              autoFocus
            >
              {discovered.map((m) => (
                <option key={m.name} value={m.name}>{m.name}</option>
              ))}
            </select>
          </div>
          <button
            type="submit"
            className="auth-submit touch-target"
            disabled={busy || !picked}
          >
            {busy ? (<><span className="spinner" /><span>Finishing…</span></>) : <span>Finish setup</span>}
          </button>
        </form>
      )}
    </div>
  );
}

// ============================================================================
// Six live setup predicates (R5 BUILD: notes section 4 Setup + section 5
// first-boot gate). GET /setup/state is the live-checked source (2 of 6
// predicates are real network/backend probes, not just row presence —
// docs/superpowers/uiv2/03-backend-gap-map.md:162); shared by the
// first-boot wizard gate below and the admin Setup page.
// ============================================================================

function _fetchSetupState() {
  return window.primerApi.apiFetch("GET", "/setup/state", null, {});
}

function _fetchCapabilities() {
  return window.primerApi.apiFetch("GET", "/capabilities", null, {});
}

// SetupPredicatesList — the six-row checklist. Renders in whichever
// visual host the caller wraps it in (the pre-login auth screens vs.
// the console's .tbl pages) — this piece is just the rows + fix
// actions, no chrome of its own, so it composes into both without a
// shared-but-wrong visual language.
function SetupPredicatesList({ state, onConfigureProvider, onRerunSeed, busy, testidPrefix }) {
  const prefix = testidPrefix || "setup-predicate";
  return (
    <ul className="setup-predicates" data-testid={prefix + "-list"}>
      {state.predicates.map((p) => {
        const isProviderRow = p.key === "llm_provider" || p.key === "model_profile";
        return (
          <li key={p.key} className={"setup-predicate" + (p.ok ? " is-ok" : " is-missing")}
            data-testid={prefix + ":" + p.key}>
            <span className={"setup-predicate-dot" + (p.ok ? " ok" : " missing")} aria-hidden="true">
              {p.ok ? "✓" : "✗"}
            </span>
            <span className="setup-predicate-label">{p.label}</span>
            {!p.ok && p.detail && (
              <span className="setup-predicate-detail muted text-sm">{p.detail}</span>
            )}
            {!p.ok && (
              <button type="button" className="sh-verb setup-predicate-fix"
                data-testid={prefix + "-fix:" + p.key}
                disabled={busy != null}
                onClick={isProviderRow ? onConfigureProvider : onRerunSeed}>
                {isProviderRow ? "Configure provider" : (busy === "seed" ? "Running…" : "Re-run seed")}
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

const _CAPABILITY_GATES = {
  huggingface: "local embedder + cross-encoder + speech models",
  lance: "local semantic-search vector store",
  docker: "container workspace backend",
  kubernetes: "kubernetes workspace backend",
  channels: "Slack / Discord / Telegram channel bridges",
};

// ============================================================================
// NV_SetupPage — the System > Setup admin surface (R5 BUILD). Six live
// predicates with fix-actions, a capabilities table, Re-run seed and
// Reset base agent roster (both REUSE — POST /setup/seed,
// POST /setup/reset_agents already existed and needed no backend change).
// ============================================================================

function NV_SetupPage() {
  const [state, setState] = React.useState(null);
  const [capabilities, setCapabilities] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [busy, setBusy] = React.useState(null); // "seed" | "reset" | null
  const [configuring, setConfiguring] = React.useState(false);

  const load = React.useCallback(() => {
    setError(null);
    Promise.all([_fetchSetupState(), _fetchCapabilities()]).then(
      ([s, c]) => { setState(s); setCapabilities(c); },
      (err) => setError(err && err.message ? err.message : String(err)),
    );
  }, []);
  React.useEffect(load, [load]);

  const rerunSeed = () => {
    setBusy("seed");
    window.primerApi.apiFetch("POST", "/setup/seed", null, {}).then(
      load, (err) => setError(err && err.message ? err.message : String(err)),
    ).finally(() => setBusy(null));
  };

  const resetRoster = () => {
    setBusy("reset");
    window.primerApi.apiFetch("POST", "/setup/reset_agents", null, {}).then(
      load, (err) => setError(err && err.message ? err.message : String(err)),
    ).finally(() => setBusy(null));
  };

  if (configuring) {
    return (
      <div data-testid="nv-sys-setup-configure">
        <SetupWizardSteps onComplete={() => { setConfiguring(false); load(); }} />
      </div>
    );
  }

  return (
    <div className="col" style={{ gap: 14 }} data-testid="nv-sys-setup-page">
      <div className="filter-bar">
        <span style={{ fontSize: 13, fontWeight: 600 }}>Setup</span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" icon="refresh" onClick={load}>Refresh</Btn>
          <Btn size="sm" kind="ghost" icon="play" onClick={rerunSeed}
            disabled={busy != null} data-testid="nv-sys-setup-rerun-seed">
            {busy === "seed" ? "Running…" : "Re-run seed"}
          </Btn>
          <Btn size="sm" kind="ghost" icon="rotate-ccw" onClick={resetRoster}
            disabled={busy != null} data-testid="nv-sys-setup-reset-roster">
            {busy === "reset" ? "Resetting…" : "Reset base agent roster"}
          </Btn>
        </div>
      </div>

      {error && <Banner kind="error" title="Couldn't load setup state" detail={error} />}

      {state && (
        <SetupPredicatesList
          state={state}
          busy={busy}
          testidPrefix="nv-sys-setup"
          onConfigureProvider={() => setConfiguring(true)}
          onRerunSeed={rerunSeed}
        />
      )}

      {capabilities && (
        <div data-testid="nv-sys-capabilities-table" className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr><th></th><th>Capability</th><th>Gates</th></tr>
            </thead>
            <tbody>
              {Object.keys(capabilities.extras).sort().map((name) => {
                const status = capabilities.extras[name];
                return (
                  <tr key={name} data-testid={"nv-sys-capability-row:" + name}>
                    <td>
                      <Icon name={status.installed ? "check" : "x-circle"}
                        className={status.installed ? undefined : "muted"} size={14} />
                    </td>
                    <td className="mono">{name}</td>
                    <td className="muted text-sm">{_CAPABILITY_GATES[name] || "—"}</td>
                  </tr>
                );
              })}
              <tr data-testid="nv-sys-capability-row:speech">
                <td>
                  <Icon
                    name={(capabilities.speech.stt_configured || capabilities.speech.tts_configured) ? "check" : "x-circle"}
                    className={(capabilities.speech.stt_configured || capabilities.speech.tts_configured) ? undefined : "muted"}
                    size={14} />
                </td>
                <td className="mono">speech</td>
                <td className="muted text-sm">
                  stt {capabilities.speech.stt_configured ? "configured" : "not configured"},{" "}
                  tts {capabilities.speech.tts_configured ? "configured" : "not configured"}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// SetupWizardGate — the first-boot host. Steps 1-2 (provider + model
// profile, unchanged) hand off to the six-predicate checklist rather
// than finishing immediately: "Enter Primer" only enables once every
// predicate passes (notes section 5), so a partially-seeded install
// (e.g. the ensure pass failed on the workspace backend) surfaces that
// instead of dropping the operator into a broken app.
// ============================================================================

function SetupWizardGate({ onDone }) {
  const [state, setState] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [busy, setBusy] = React.useState(null); // "seed" | null
  // null = not yet routed. Decided once, from the first load: a fresh
  // install (no provider/profile) lands on the 2-step form same as
  // before; a RETURNING admin whose provider/profile already exist
  // (e.g. a prior ensure pass failed on the workspace backend) lands
  // straight on the checklist instead of redoing provider setup.
  const [configuring, setConfiguring] = React.useState(null);

  const load = React.useCallback(() => {
    setError(null);
    return _fetchSetupState().then(setState, (err) => (
      setError(err && err.message ? err.message : String(err))
    ));
  }, []);
  React.useEffect(() => { load(); }, [load]);
  React.useEffect(() => {
    if (configuring !== null || !state) return;
    const providerMissing = state.predicates.some(
      (p) => (p.key === "llm_provider" || p.key === "model_profile") && !p.ok,
    );
    setConfiguring(providerMissing);
  }, [state, configuring]);

  const rerunSeed = () => {
    setBusy("seed");
    window.primerApi.apiFetch("POST", "/setup/seed", null, {}).then(
      load, (err) => setError(err && err.message ? err.message : String(err)),
    ).finally(() => setBusy(null));
  };

  const afterProviderStep = () => {
    setConfiguring(false);
    // Seeding needs the profile step 2 just created (amendment C3).
    rerunSeed();
  };

  if (configuring) {
    return (
      <div className="auth-shell">
        <div className="auth-wrap">
          <div className="auth-card">
            <div className="auth-h">
              <h1 className="title">Configure this install</h1>
              <div className="sub">
                Pick a model provider and a default model. Everything else you can
                ask the operator for once you are inside.
              </div>
            </div>
            <SetupWizardSteps onComplete={afterProviderStep} />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="auth-shell">
      <div className="auth-wrap">
        <div className="auth-card">
          <div className="auth-h">
            <h1 className="title">Finish setup</h1>
            <div className="sub">
              Every check below must pass before you can enter Primer.
            </div>
          </div>
          {error && (
            <div className="auth-banner">
              <div style={{ flex: 1 }}>
                <div className="title">Couldn't load setup state</div>
                <div className="detail">{error}</div>
              </div>
            </div>
          )}
          {!state && !error && <div className="muted" style={{ padding: 20 }}>Loading…</div>}
          {state && (
            <SetupPredicatesList
              state={state}
              busy={busy}
              testidPrefix="setup-gate-predicate"
              onConfigureProvider={() => setConfiguring(true)}
              onRerunSeed={rerunSeed}
            />
          )}
          <button
            type="button"
            className="auth-submit touch-target"
            disabled={!state || !state.complete}
            data-testid="setup-gate-enter"
            onClick={onDone}
            style={{ marginTop: 12 }}
          >
            Enter Primer
          </button>
        </div>
      </div>
    </div>
  );
}

function SetupWaitingScreen({ username }) {
  return (
    <div className="auth-shell">
      <div className="auth-wrap">
        <div className="auth-card">
          <div className="auth-h">
            <h1 className="title">Setup in progress</h1>
            <div className="sub">
              {username ? <>Signed in as <span className="mono">{username}</span>. </> : null}
              An administrator is still configuring this install. This page
              works as soon as they finish.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

window.SetupWizardSteps = SetupWizardSteps;
window.SetupWizardGate = SetupWizardGate;
window.SetupWaitingScreen = SetupWaitingScreen;
window.SetupPredicatesList = SetupPredicatesList;
window.NV_SetupPage = NV_SetupPage;
