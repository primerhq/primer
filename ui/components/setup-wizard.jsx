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

// urlHint is the example under Base URL (a type that takes no URL has none); needsKey marks the hosted providers, which cannot be
// used without a key, where a self-hosted server may run without one (admin review ADM-03, ADM-05).
const SETUP_PROVIDER_TYPES = [
  { id: "openchat", label: "OpenAI-compatible (chat completions)", needsUrl: true, urlHint: "https://api.openai.com/v1", needsKey: false },
  { id: "openresponses", label: "OpenAI-compatible (responses)", needsUrl: true, urlHint: "https://api.openai.com/v1", needsKey: false },
  { id: "ollama", label: "Ollama", needsUrl: true, urlHint: "http://localhost:11434", needsKey: false },
  { id: "anthropic", label: "Anthropic", needsUrl: false, urlHint: "", needsKey: true },
  { id: "gemini", label: "Gemini", needsUrl: false, urlHint: "", needsKey: true },
  { id: "openrouter", label: "OpenRouter", needsUrl: false, urlHint: "", needsKey: true },
];

function _setupDraftConfig(type, url, apiKey) {
  const spec = SETUP_PROVIDER_TYPES.find((t) => t.id === type);
  const config = {};
  if (spec && spec.needsUrl && url) config.url = url;
  if (apiKey) config.api_key = apiKey;
  return config;
}

// ---- resume helpers: which step is a fact about the SERVER, not about React state ------------------------------------------------
//
// Step 1 saves the provider row (id "llm-<type>") before step 2 exists, and the setup gate sends the operator back into this wizard
// for as long as the model profile is missing. So a reload, a second tab or an abandoned run all arrive with the provider already
// saved. The helpers below (JSX-free, run for real in tests/ui/test_setup_wizard.py) decide where the wizard starts from what is
// saved, and make both saves idempotent, so the wizard has no state it cannot re-enter (console review 2026-10-08, C-001).

function SW_providerId(type) {
  return "llm-" + type;
}

// What the wizard should show on entry, from the saved providers and model profiles.
//   no provider                          -> step 1
//   a provider with no profile           -> step 2 for that provider (the profile is what is left to do)
//   every provider already has a profile -> step 1 prefilled with the first one: the gate re-entered because it no longer
//                                           answers, and the operator must be able to correct it, not be told it exists
function SW_resumePlan(providers, profiles) {
  var provs = providers || [];
  if (!provs.length) return { step: 1 };
  var covered = {};
  (profiles || []).forEach(function (pr) { covered[pr.provider_id] = true; });
  var pending = provs.filter(function (pv) { return !covered[pv.id]; });
  if (pending.length) return { step: 2, providerId: pending[0].id };
  var first = provs[0];
  return { step: 1, prefill: { type: first.provider, url: (first.config && first.config.url) || "", providerId: first.id } };
}

// The update body for a provider whose row already exists. PUT is a full replace and a deliberately blanked optional secret CLEARS
// the stored credential (only a mask-shaped echo is kept), but this form never shows a stored key, so a blank field means "I did
// not touch it", not "remove it": carry the served mask of the stored key and the server restores the real one. A key the operator
// typed wins; a row with no key stays without one.
function SW_keepStoredKey(body, stored) {
  var draft = body && body.config ? body.config.api_key : undefined;
  var served = stored && stored.config ? stored.config.api_key : null;
  if (typeof draft === "string" && draft.trim() !== "") return body;
  if (!served) return body;
  return Object.assign({}, body, { config: Object.assign({}, body.config, { api_key: served }) });
}

// Create the provider, or update it when the id is already there (a 409 is the row from an earlier run or an earlier step 1).
function SW_saveProvider(apiFetch, body) {
  return apiFetch("POST", "/llm_providers", body, {}).catch(function (err) {
    if (!(err && err.status === 409)) throw err;
    var path = "/llm_providers/" + encodeURIComponent(body.id);
    var typedAKey = !!(body && body.config && typeof body.config.api_key === "string" && body.config.api_key.trim() !== "");
    // Read the stored row only when a key could be lost: without knowing what is stored, a blind PUT might erase it, so a
    // failed read stops the update.
    var stored = typedAKey ? Promise.resolve(null) : apiFetch("GET", path, null, {});
    return stored.then(function (row) {
      return apiFetch("PUT", path, SW_keepStoredKey(body, row), {});
    });
  });
}

// Create the profile; one that already exists is the retry of a request whose answer was lost, which is success.
function SW_saveProfile(apiFetch, body) {
  return apiFetch("POST", "/model_profiles", body, {}).catch(function (err) {
    if (err && err.status === 409) return { exists: true };
    throw err;
  });
}

// The plan with what step 2 needs. The model list comes from a live probe of the SAVED provider: its secret is redacted on the wire,
// so replaying the stored row to the draft probe could not authenticate. A saved provider that does not answer sends the operator
// to step 1, prefilled, with a notice that says which one and why. The same probe tells the operator why the gate sent them here
// when every provider already has its profile. Never blocks the wizard: any failure reading the state is "start at step 1".
function SW_loadResume(apiFetch) {
  return Promise.all([
    apiFetch("GET", "/llm_providers?limit=200", null, {}),
    apiFetch("GET", "/model_profiles?limit=200", null, {}),
  ]).then(function (res) {
    var providers = (res[0] && res[0].items) || [];
    var plan = SW_resumePlan(providers, (res[1] && res[1].items) || []);
    if (!plan.providerId && !plan.prefill) return plan;
    var id = plan.providerId || plan.prefill.providerId;
    var row = providers.filter(function (pv) { return pv.id === id; })[0];
    var prefill = plan.prefill || { type: row.provider, url: (row.config && row.config.url) || "", providerId: row.id };
    var edit = " Check its details and connect again to update it.";
    return apiFetch("GET", "/llm_providers/" + encodeURIComponent(id) + "/discovered_models", null, {}).then(
      function (probe) {
        var models = (probe && probe.models) || [];
        if (plan.step === 2) {
          if (models.length) return { step: 2, providerId: id, models: models };
          return { step: 1, prefill: prefill, notice: "The saved provider " + id + " answered but listed no models." + edit };
        }
        return plan;
      },
      function (err) {
        // The probe error of a saved provider can carry its Base URL's credentials (httpx echoes the URL): clean it like every other failure.
        var why = SW_tidy((err && (err.detail || err.message)) || "") || "no reason given";
        return { step: 1, prefill: prefill, notice: "The saved provider " + id + " did not answer: " + why + "." + edit };
      }
    );
  }).catch(function () { return { step: 1 }; });
}

// ---- what the operator is told when step 1 cannot go on (admin review ADM-02, ADM-03, ADM-05) -----------------------------------------
//
// POST /llm_providers/_discover_models answers EVERY failure with a 400 and a plain-text detail, so only the text tells a draft the
// provider model rejected ("Draft provider failed validation: ..." or "invalid <X> config: ...", pydantic's dump) from the upstream's
// own failure ("<X> probe failed: ...", "<X> discover failed: HTTP n ...", "<X> discover network error: ..."). The class is read from
// that text here, in one place, and tests/ui/test_setup_wizard_failures.py feeds it the REAL backend's wording, so a change there turns
// that file red instead of quietly returning the wizard to one generic title.

function SW_typeSpec(type) {
  return SETUP_PROVIDER_TYPES.filter(function (t) { return t.id === type; })[0] || null;
}

// The example under Base URL for the selected type ("" for a type that takes none).
function SW_urlHint(type) {
  var spec = SW_typeSpec(type);
  return spec && spec.needsUrl ? spec.urlHint : "";
}

// The API key placeholder: a hosted provider cannot be used without one, a self-hosted server may run without.
function SW_keyHint(type) {
  var spec = SW_typeSpec(type);
  return spec && spec.needsKey ? "required for this provider" : "leave blank for unauthenticated servers";
}

// What pressing Connect with a required field empty says, or null. The button is not disabled for it: a disabled button never says why.
function SW_missingField(type, url, apiKey) {
  var spec = SW_typeSpec(type);
  if (!spec) return null;
  if (spec.needsUrl && !String(url || "").trim()) {
    return { field: "url", title: "Enter the server's address", message: "A full URL, for example " + spec.urlHint };
  }
  if (spec.needsKey && !String(apiKey || "").trim()) {
    return { field: "apiKey", title: "Enter the API key", message: "Paste the key from the provider's dashboard." };
  }
  return null;
}

// A backend detail without the links, pydantic's "[type=..., input_value=..., input_type=...]" noise (which also echoes the input) and
// the credentials of any URL in it: a Base URL such as http://user:pass@host/v1 comes back in httpx's error message, and a secret must not
// be printed into a banner. The userinfo runs to the LAST "@" before the first "/", because a password may contain one, and it may contain an
// apostrophe too (RFC 3986 allows it, pydantic's HttpUrl accepts it unencoded and httpx keeps it raw in its error), so the class does not stop at
// one; only a space, a slash or a double quote ends it.
function SW_tidy(text) {
  return String(text || "")
    .replace(/(\b[a-z][a-z0-9+.-]*:\/\/)[^\/\s"]*@/gi, "$1")
    .replace(/\s*For further information visit \S+/g, "")
    .replace(/\s*For more information check: \S+/g, "")
    .replace(/\s*\[type=[^\n]*?input_type=[^\]\n]*\]/g, "")
    .trim();
}

// The text of an error shown on the Setup page or the gate: what the console always showed (the error's message, or the error itself as text), cleaned.
function SW_errorText(err) {
  return SW_tidy(err && err.message ? err.message : String(err));
}

// pydantic's "<loc>\n  <reason>" pairs from a validation dump: [{loc, reason}].
function SW_validationFields(text) {
  var tidy = SW_tidy(text);
  var re = /\n(\S+)\n[ \t]+([^\n]+)/g;
  var out = [];
  var m;
  while ((m = re.exec(tidy))) out.push({ loc: m[1], reason: m[2].trim() });
  return out;
}

// The HTTP status a backend detail reports, or "" when it names none. Each wording is read where the backend puts it, so a status quoted in
// the SERVER'S words (a body, a proxy's page) cannot replace the real one: the hosted providers say "<X> discover failed: HTTP n ..." at the
// START of the message, so that is read first and only there (a hosted-looking phrase quoted mid-text, or a body that ends in "(status code:
// n)", is not theirs); ollama's message ends in "(status code: n)" after the server's words, so that suffix comes next; Gemini's key message
// says "(HTTP n)"; and httpx says "Client error 'n Reason' for url ..." (or Server or Redirect). tests/ui/test_setup_wizard_failures.py feeds
// it each wording.
function SW_httpStatus(raw) {
  var m = /^\w+ discover failed:\s+HTTP\s+(\d{3})\b/.exec(raw)
    || /\(status code:\s*(\d{3})\)\s*$/.exec(raw)
    || /\(HTTP\s+(\d{3})\)/.exec(raw)
    || /\b(?:Client|Server|Redirect) error '(\d{3}) [A-Za-z ]+'/.exec(raw);
  return m ? m[1] : "";
}

// {title, detail, field, message} for a failed probe: the title says what failed; a problem with one input is shown under that input
// (field "url" | "apiKey", with its message) instead of in a banner; anything else keeps its tidied detail in the banner. apiKey is the
// key that was SENT: a 401 or 403 with none is a missing key, not a rejected one.
function SW_probeFailure(err, type, apiKey) {
  var raw = String((err && (err.detail || err.message)) || "").trim();
  if (/^(Draft provider failed validation|invalid [\w ]+ config):/i.test(raw)) {
    var fields = SW_validationFields(raw);
    var urlProblem = fields.filter(function (f) { return /(^|\.)url$/.test(f.loc); })[0];
    var keyProblem = fields.filter(function (f) { return /(^|\.)api_key$/.test(f.loc); })[0];
    if (urlProblem) {
      var missing = /Field required/i.test(urlProblem.reason);
      var example = SW_urlHint(type);
      return {
        field: "url", detail: "",
        title: missing ? "Enter the server's address" : "That address is not valid",
        message: example ? "A full URL, for example " + example : "A full URL, starting with http:// or https://",
      };
    }
    if (keyProblem) {
      return { field: "apiKey", detail: "", title: "Enter the API key", message: "Paste the key from the provider's dashboard." };
    }
    return {
      field: null, message: "", title: "Those settings are not valid",
      detail: fields.length
        ? fields.map(function (f) { return f.loc + ": " + f.reason; }).join("; ")
        : SW_tidy(raw.replace(/^[^:]+:\s*/, "")),
    };
  }
  // The HTTP status comes first: a body that merely CONTAINS "unauthorized" or "forbidden" (a 404 page, a 500 trace) is not a key problem.
  // The words only count when there is no status at all, and only as whole words.
  var code = SW_httpStatus(raw);
  var denied = code === "401" || code === "403" ||
    (!code && /\b(?:unauthori[sz]ed|forbidden)\b|\bkey invalid\b/i.test(raw));
  if (denied) {
    // A server that wants a key, reached without one (the self-hosted types may run without), is a MISSING key, shown under the field.
    if (!String(apiKey || "").trim()) {
      return {
        field: "apiKey", title: "This provider needs an API key", detail: SW_tidy(raw),
        message: "The server refused a request that carried no key. Paste the key it expects.",
      };
    }
    // The server's own words follow the advice: a proxy or WAF that answers 403 is not necessarily talking about the key.
    return { field: null, message: "", title: "The provider rejected the API key", detail: ("Check the key and connect again. " + SW_tidy(raw)).trim() };
  }
  if (code) {
    // The /v1 hint is for a type whose Base URL ends in /v1 (the OpenAI-compatible ones): not Ollama, not a hosted provider with no Base URL.
    var more = code === "404" && /\/v1$/.test(SW_urlHint(type))
      ? " Check the Base URL: an OpenAI-compatible server usually answers at an address ending in /v1."
      : "";
    return { field: null, message: "", title: "The provider answered with an error", detail: SW_tidy(raw) + more };
  }
  return { field: null, message: "", title: "Could not reach that provider", detail: SW_tidy(raw) };
}
// ---- end of the resume helpers

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
  // A problem with one input, shown under it: { url: "...", apiKey: "..." } (admin review ADM-02, ADM-05).
  const [fieldErr, setFieldErr] = React.useState({});
  // The docs harness starts a capture at a given step and never reads the server; the console reads what is already saved first.
  const [ready, setReady] = React.useState(!!initialStep);
  const [resumed, setResumed] = React.useState(false);

  React.useEffect(() => {
    if (initialStep) return undefined;
    let cancelled = false;
    SW_loadResume(window.primerApi.apiFetch).then((plan) => {
      if (cancelled) return;
      if (plan.step === 2) {
        setProviderId(plan.providerId);
        setDiscovered(plan.models);
        setPicked(plan.models[0].name);
        setStep(2);
        setResumed(true);
      } else if (plan.prefill) {
        if (SETUP_PROVIDER_TYPES.some((t) => t.id === plan.prefill.type)) setType(plan.prefill.type);
        setUrl(plan.prefill.url || "");
      }
      if (plan.notice) setErr({ title: "The saved provider did not answer", detail: plan.notice });
      setReady(true);
    });
    return () => { cancelled = true; };
  }, []);

  const spec = SETUP_PROVIDER_TYPES.find((t) => t.id === type);

  // Editing a field (or the type) answers a failure that was about a field: the message under it goes, and so does the banner that named it.
  // A banner that is not about a field (the resume notice, an unreachable provider) stays.
  const clearFieldFailure = () => {
    if (Object.keys(fieldErr).length === 0) return;
    setFieldErr({});
    setErr(null);
  };

  // Step 1: a successful draft probe IS the proof that the provider works, so the provider row is only persisted once the probe
  // returns models. The save is idempotent (an existing row is updated), and each phase reports under its own title.
  const submitProvider = async (e) => {
    e.preventDefault();
    setErr(null);
    setFieldErr({});
    // A required field left empty is named here, not by a disabled button and not by a round trip.
    const missing = SW_missingField(type, url, apiKey);
    if (missing) {
      setErr({ title: missing.title });
      setFieldErr({ [missing.field]: missing.message });
      return;
    }
    setBusy(true);
    try {
      const config = _setupDraftConfig(type, url, apiKey);
      let models;
      try {
        const probe = await window.primerApi.apiFetch(
          "POST", "/llm_providers/_discover_models",
          { provider: type, config }, {},
        );
        models = (probe && probe.models) || [];
      } catch (e2) {
        // The title says what failed (SW_probeFailure reads the backend's text); a bad input is shown under that input.
        const failure = SW_probeFailure(e2, type, apiKey);
        setErr({ title: failure.title, detail: failure.detail });
        if (failure.field) setFieldErr({ [failure.field]: failure.message });
        return;
      }
      if (!models.length) {
        setErr({ title: "No models returned", detail: "The provider answered but listed no models." });
        return;
      }
      const id = SW_providerId(type);
      try {
        await SW_saveProvider(
          window.primerApi.apiFetch,
          { id, provider: type, config, limits: { max_concurrency: 4 } },
        );
      } catch (e3) {
        setErr({ title: "Could not save the provider", detail: SW_tidy(e3 && (e3.detail || e3.message)) });
        return;
      }
      setProviderId(id);
      setDiscovered(models);
      setPicked(models[0].name);
      setStep(2);
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
      await SW_saveProfile(window.primerApi.apiFetch, {
        id: providerId + "--" + model.name,
        description: "Default profile created by first-run setup.",
        provider_id: providerId,
        model_name: model.name,
        context_length: model.context_length || 32000,
      });
      await onComplete();
    } catch (e2) {
      setErr({ title: "Could not register that model", detail: SW_tidy(e2 && (e2.detail || e2.message)) });
    } finally {
      setBusy(false);
    }
  };

  if (!ready) {
    return (
      <div className="setup-steps">
        <div className="setup-progress mono">Checking this install…</div>
      </div>
    );
  }

  return (
    <div className="setup-steps">
      <div className="setup-progress mono">
        Step {step} of 2{resumed ? " · continuing with the provider you saved (" + providerId + ")" : ""}
      </div>
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
              onChange={(e) => { setType(e.target.value); clearFieldFailure(); }}
            >
              {SETUP_PROVIDER_TYPES.map((t) => (
                <option key={t.id} value={t.id}>{t.label}</option>
              ))}
            </select>
          </div>
          {spec && spec.needsUrl && (
            <div className={"auth-field" + (fieldErr.url ? " has-err" : "")}>
              <label htmlFor="setup-url">Base URL</label>
              <input
                id="setup-url"
                className="mono"
                value={url}
                onChange={(e) => { setUrl(e.target.value); clearFieldFailure(); }}
                placeholder={SW_urlHint(type)}
                aria-invalid={!!fieldErr.url}
                autoFocus
              />
              {fieldErr.url && <div className="field-err" data-testid="setup-url-error">{fieldErr.url}</div>}
            </div>
          )}
          <div className={"auth-field" + (fieldErr.apiKey ? " has-err" : "")}>
            <label htmlFor="setup-key">API key</label>
            <input
              id="setup-key"
              className="mono"
              type="password"
              value={apiKey}
              onChange={(e) => { setApiKey(e.target.value); clearFieldFailure(); }}
              placeholder={SW_keyHint(type)}
              aria-invalid={!!fieldErr.apiKey}
            />
            {fieldErr.apiKey && <div className="field-err" data-testid="setup-key-error">{fieldErr.apiKey}</div>}
          </div>
          <button
            type="submit"
            className="auth-submit touch-target"
            disabled={busy}
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
              <span className="setup-predicate-detail muted text-sm">{SW_tidy(p.detail)}</span>
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
      (err) => setError(SW_errorText(err)),
    );
  }, []);
  React.useEffect(load, [load]);

  const rerunSeed = () => {
    setBusy("seed");
    window.primerApi.apiFetch("POST", "/setup/seed", null, {}).then(
      load, (err) => setError(SW_errorText(err)),
    ).finally(() => setBusy(null));
  };

  const resetRoster = () => {
    setBusy("reset");
    window.primerApi.apiFetch("POST", "/setup/reset_agents", null, {}).then(
      load, (err) => setError(SW_errorText(err)),
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
      setError(SW_errorText(err))
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
    // A failed seed still re-reads the state, so the checklist shows what is true (and its Re-run seed) instead of whatever was read last.
    window.primerApi.apiFetch("POST", "/setup/seed", null, {}).then(
      load, (err) => load().then(() => setError(SW_errorText(err))),
    ).finally(() => setBusy(null));
  };

  const afterProviderStep = () => {
    setConfiguring(false);
    // The state in hand was read BEFORE the wizard ran and says no provider is configured; drawing it until the post-seed read arrives showed a red
    // failure right after the operator fixed it (console review C-039). No state is "Loading..." and the Enter button is off.
    setState(null);
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
