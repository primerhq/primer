/* global React, Icon, Btn, Banner */
// ============================================================================
// One parameterized provider form (S4 P2 Task 19).
//
// Every provider class previously carried its own form component with its
// own field list. They are all the same shape: a provider-type picker, a
// few row-level fields, a config block, and limits. So the field list
// comes from the class's own /_types endpoint and exactly one component
// knows how to render it.
//
// Capability hints render INSIDE this form using the existing
// foundation/capabilities.js helpers, which are deliberately untouched.
// ============================================================================

// Field name to input type. Anything not named here renders as text.
const PC_FIELD_TYPES = {
  "api_key": "password",
  "token": "password",
  "password": "password",
  "secret_access_key": "password",
  "max_concurrency": "number",
  "connect_timeout_seconds": "number",
  "request_timeout_seconds": "number",
  "max_retries": "number",
  "port": "number",
};

function PC_Field({ name, value, onChange, disabled }) {
  const kind = PC_FIELD_TYPES[name] || "text";
  return (
    <div className="field">
      <label className="field-label mono text-sm" htmlFor={`pf-${name}`}>
        {name}
      </label>
      <input
        id={`pf-${name}`}
        className="input mono"
        type={kind}
        disabled={disabled}
        value={value == null ? "" : value}
        onChange={(e) => {
          const raw = e.target.value;
          onChange(name, kind === "number" && raw !== "" ? Number(raw) : raw);
        }}
      />
    </div>
  );
}

function PC_ProviderForm({ plural, typesPath, value, onChange, onSubmit, onTest }) {
  const { useResource, apiFetch, useCapabilities, capabilityHint } = window.primerApi;
  const EXTRA_FOR_PROVIDER_TYPE = window.primerApi.EXTRA_FOR_PROVIDER_TYPE || {};
  const caps = useCapabilities();
  const [testResult, setTestResult] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  const types = useResource(
    `pf:types:${plural}`,
    (signal) => apiFetch("GET", typesPath || `/${plural}/_types`, null, { signal }),
    { pollMs: null },
  );

  const draft = value || {};
  const entries = types.data?.types || types.data?.items || [];
  const selectedType = draft.provider || (entries[0] && entries[0].provider) || "";
  const spec = entries.find((t) => t.provider === selectedType) || entries[0] || {};
  const rowFields = spec.row_fields || [];
  const configFields = spec.config_fields || [];

  // The provider type decides whether an optional extra is needed; the
  // hint text comes from the shared capabilities helper so the wording
  // stays identical to every other surface that gates on extras.
  const extra = EXTRA_FOR_PROVIDER_TYPE[selectedType];
  const missingExtra = extra && caps && caps.extras && !caps.extras[extra]?.installed;

  const setField = (name, v) => onChange({ ...draft, [name]: v });
  const setConfig = (name, v) =>
    onChange({ ...draft, config: { ...(draft.config || {}), [name]: v } });

  const runTest = async () => {
    setBusy(true);
    try {
      const out = typeof onTest === "function"
        ? await onTest(draft)
        : await apiFetch("POST", `/${plural}/_test`, draft);
      setTestResult(out);
    } catch (err) {
      setTestResult({ ok: false, error: err?.detail || err?.message || String(err) });
    } finally {
      setBusy(false);
    }
  };

  if (types.error) {
    return (
      <Banner kind="error" title="Could not load the provider types">
        {String(types.error.detail || types.error.message || types.error)}
      </Banner>
    );
  }

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="field">
        <label className="field-label" htmlFor="pf-provider">provider</label>
        <select
          id="pf-provider"
          className="input"
          value={selectedType}
          onChange={(e) => setField("provider", e.target.value)}
        >
          {entries.map((t) => (
            <option key={t.provider} value={t.provider}>{t.provider}</option>
          ))}
        </select>
      </div>

      {missingExtra ? (
        <Banner kind="info" title="Optional dependency required">
          {capabilityHint(extra)}
        </Banner>
      ) : null}

      <PC_Field name="id" value={draft.id} onChange={setField} />

      {rowFields.map((name) => (
        <PC_Field key={name} name={name} value={draft[name]} onChange={setField} />
      ))}

      {configFields.length ? (
        <div className="col" style={{ gap: 8 }}>
          <div className="muted text-sm">config</div>
          {configFields.map((name) => (
            <PC_Field
              key={name}
              name={name}
              value={(draft.config || {})[name]}
              onChange={setConfig}
            />
          ))}
        </div>
      ) : null}

      {testResult ? (
        <Banner
          kind={testResult.ok ? "success" : "error"}
          title={testResult.ok ? "Reachable" : "Test failed"}
        >
          {testResult.ok
            ? (testResult.models || testResult.voices || []).join(", ")
            : testResult.error}
        </Banner>
      ) : null}

      <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
        <Btn
          kind="ghost"
          data-testid="provider-form-test"
          onClick={runTest}
          disabled={busy}
        >
          Test
        </Btn>
        <Btn onClick={() => onSubmit && onSubmit(draft)} disabled={busy}>Save</Btn>
      </div>
    </div>
  );
}

window.PC_FIELD_TYPES = PC_FIELD_TYPES;
window.PC_ProviderForm = PC_ProviderForm;
