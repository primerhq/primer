/* global React, Icon, Btn, Banner */
// ============================================================================
// One parameterized provider form (S4 P2 Task 19, P4 Task 29).
//
// Every provider class previously carried its own form component with its
// own field list. They are all the same shape: a provider-type picker, a
// few row-level fields, a config block, and limits. So the field list
// comes from the class's own /_types endpoint and exactly one component
// knows how to render it. No field table lives here: the place that owns
// the provider enums is the place that describes their fields.
//
// Capability hints render INSIDE this form using the existing
// foundation/capabilities.js helpers, which are deliberately untouched.
// ============================================================================

// Field name to input type. A served descriptor carries its own `type`;
// this is the fallback for the bare-name shape.
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

function PC_fieldType(name) {
  return PC_FIELD_TYPES[name] || "text";
}

// _types answers in two shapes. web_search, web_fetch and the speech
// classes serve bare field NAMES (routers/web_search.py:216-222); the
// model-family classes serve DESCRIPTORS (routers/providers.py
// list_llm_provider_types). Normalise once so one renderer covers both.
function PC_normalizeField(field) {
  if (typeof field === "string") {
    return {
      key: field,
      label: field,
      type: PC_fieldType(field),
      required: false,
      help: "",
      options: null,
      placeholder: "",
    };
  }
  return {
    key: field.key,
    label: field.label || field.key,
    type: field.type || PC_fieldType(field.key),
    required: !!field.required,
    help: field.help || "",
    options: field.options || null,
    placeholder: field.placeholder || "",
  };
}

// EmbeddingProvider and CrossEncoderProvider declare models: min_length=1,
// so the row is rejected outright without one. A comma-joined text box
// would round-trip badly (names contain slashes and dots), so this is a
// real add/remove list of {name} objects, matching the wire shape.
function PC_ModelListField({ value, onChange }) {
  const rows = Array.isArray(value) ? value : [];
  const setName = (i, name) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, name } : r)));
  return (
    <div className="model-list" data-testid="provider-form-model-list">
      {rows.map((row, i) => (
        <div className="model-list-row" key={i}>
          <input
            type="text"
            className="mono"
            value={row.name || ""}
            onChange={(e) => setName(i, e.target.value)}
          />
          <Btn
            type="button"
            kind="ghost"
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
          >
            Remove
          </Btn>
        </div>
      ))}
      <Btn
        type="button"
        data-testid="provider-form-add-model"
        onClick={() => onChange(rows.concat([{ name: "" }]))}
      >
        Add model
      </Btn>
    </div>
  );
}

function PC_Field({ field, value, onChange }) {
  let input;
  if (field.type === "model_list") {
    input = <PC_ModelListField value={value} onChange={onChange} />;
  } else if (field.type === "enum") {
    input = (
      <select value={value || ""} onChange={(e) => onChange(e.target.value)}>
        {(field.options || []).map((opt) => (
          <option key={opt} value={opt}>{opt}</option>
        ))}
      </select>
    );
  } else {
    input = (
      <input
        type={field.type}
        required={field.required}
        placeholder={field.placeholder}
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <label className="field" data-field={field.key}>
      <span>
        {field.label}
        {field.required ? <em className="req"> *</em> : null}
      </span>
      {input}
      {field.help ? <span className="field-help">{field.help}</span> : null}
    </label>
  );
}

// Limits is required on every model-family provider row and
// max_concurrency inside it has no default, so the form has to offer it.
// The optional timeouts are left blank by default: an empty box means
// "use the model default", which is what the backend's None means.
function PC_LimitsFieldset({ value, onChange }) {
  const limits = value || { max_concurrency: 1 };
  const set = (key, raw) => {
    const next = { ...limits };
    if (raw === "") delete next[key];
    else next[key] = Number(raw);
    onChange(next);
  };
  return (
    <fieldset className="limits" data-testid="provider-form-limits">
      <legend>Limits</legend>
      <label className="field">
        <span>max_concurrency<em className="req"> *</em></span>
        <input
          type="number"
          min="1"
          value={limits.max_concurrency == null ? 1 : limits.max_concurrency}
          onChange={(e) => set("max_concurrency", e.target.value || "1")}
        />
      </label>
      <label className="field">
        <span>request_timeout_seconds</span>
        <input
          type="number"
          min="0"
          value={limits.request_timeout_seconds == null ? "" : limits.request_timeout_seconds}
          onChange={(e) => set("request_timeout_seconds", e.target.value)}
        />
      </label>
      <label className="field">
        <span>connect_timeout_seconds</span>
        <input
          type="number"
          min="0"
          value={limits.connect_timeout_seconds == null ? "" : limits.connect_timeout_seconds}
          onChange={(e) => set("connect_timeout_seconds", e.target.value)}
        />
      </label>
    </fieldset>
  );
}

function PC_ProviderForm({ plural, typesPath, value, onChange, onSubmit, onTest }) {
  const { useResource, apiFetch, useCapabilities, capabilityHint,
    EXTRA_FOR_PROVIDER_TYPE } = window.primerApi;
  const [busy, setBusy] = React.useState(false);
  const [testResult, setTestResult] = React.useState(null);
  const caps = (useCapabilities() || {}).data;
  const types = useResource(
    `provider-types:${plural}`,
    (signal) => apiFetch("GET", typesPath || `/${plural}/_types`, null, { signal }),
    { pollMs: null },
  );

  const draft = value || {};
  // _types answers a MAP keyed by provider-type value.
  const typeMap = types.data || {};
  const typeKeys = Object.keys(typeMap);
  const selectedType = draft.provider || typeKeys[0] || "";
  const shape = typeMap[selectedType] || {};

  // The provider type decides whether an optional extra is needed; the
  // hint text comes from the shared capabilities helper so the wording
  // stays identical to every other surface that gates on extras.
  const extra = EXTRA_FOR_PROVIDER_TYPE[selectedType];
  const missingExtra = extra && caps && caps.extras && !caps.extras[extra]?.installed;

  const setField = (scope, name, v) => {
    if (scope === "config") {
      onChange({ ...draft, config: { ...(draft.config || {}), [name]: v } });
      return;
    }
    onChange({ ...draft, [name]: v });
  };

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
          onChange={(e) => setField("row", "provider", e.target.value)}
        >
          {typeKeys.map((k) => (
            <option key={k} value={k}>{(typeMap[k] || {}).label || k}</option>
          ))}
        </select>
      </div>

      {missingExtra ? (
        <Banner kind="info" title="Optional dependency required">
          {capabilityHint(extra)}
        </Banner>
      ) : null}

      <PC_Field
        field={PC_normalizeField({ key: "id", label: "id", required: true })}
        value={draft.id}
        onChange={(next) => setField("row", "id", next)}
      />

      {(shape.row_fields || []).map(PC_normalizeField).map((f) => (
        <PC_Field
          key={`row:${f.key}`}
          field={f}
          value={draft[f.key]}
          onChange={(next) => setField("row", f.key, next)}
        />
      ))}

      {(shape.config_fields || []).map(PC_normalizeField).map((f) => (
        <PC_Field
          key={`config:${f.key}`}
          field={f}
          value={(draft.config || {})[f.key]}
          onChange={(next) => setField("config", f.key, next)}
        />
      ))}

      {shape.limits && (
        <PC_LimitsFieldset
          value={draft.limits}
          onChange={(next) => setField("row", "limits", next)}
        />
      )}

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
window.PC_normalizeField = PC_normalizeField;
window.PC_ProviderForm = PC_ProviderForm;
