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

function SetupWizardGate({ onDone }) {
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
          <SetupWizardSteps
            onComplete={async () => {
              // Seeding needs the profile step 2 just created (amendment C3).
              try {
                await window.primerApi.apiFetch("POST", "/setup/seed", null, {});
              } catch (e) {
                /* the next boot's ensure pass repairs it; reload regardless */
              }
              onDone();
            }}
          />
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
