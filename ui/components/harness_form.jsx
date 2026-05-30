/* global React, Icon, Btn */
// JsonSchemaForm — recursive JSON-Schema renderer used by HarnessRegisterDialog
// (overrides form). Prefix HF_ to avoid global name collisions.

// ============================================================================
// Custom widget: provider picker (generic)
// ============================================================================

function HF_ProviderPicker({ endpoint, value, onChange, label }) {
  const { useResource, apiFetch } = window.primerApi;
  const key = "hf-provider-picker:" + endpoint;
  const res = useResource(
    key,
    (signal) => apiFetch("GET", endpoint + "?limit=200", null, { signal }),
    { pollMs: null }
  );
  const items = res.data?.items ?? [];
  return (
    <select
      className="select"
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
      style={{ width: "100%" }}
    >
      <option value="">— select {label} —</option>
      {items.map((item) => (
        <option key={item.id} value={item.id}>{item.id}</option>
      ))}
    </select>
  );
}

// ============================================================================
// JsonSchemaForm — recursive renderer
// ============================================================================

function JsonSchemaForm({ schema, value, onChange, errors, _path }) {
  const { useViewport } = window.primerApi;
  const { isMobile } = useViewport();
  if (!schema) return null;
  const path = _path || "";
  const type = schema.type;
  const widget = schema["x-primer-widget"];

  // Only the top-level (root) call has no _path. Wrap that single
  // render in a div carrying the mobile-aware class so children stack
  // single-column at narrow viewports.
  const isRoot = !_path;
  const renderInner = (node) => isRoot ? (
    <div className={`harness-form ${isMobile ? "harness-form-mobile" : ""}`}>
      {node}
    </div>
  ) : node;

  // Custom widgets take priority
  if (widget === "llm-provider-picker") {
    const err = (errors || []).find((e) => e.path === path);
    return renderInner(
      <div className="field">
        {schema.title && <label className="field-label">{schema.title}</label>}
        <HF_ProviderPicker endpoint="/v1/llm_providers" value={value} onChange={onChange} label={schema.title || "LLM provider"} />
        {err && <div className="field-help" style={{ color: "var(--red)" }}>{err.message}</div>}
        {schema.description && <div className="field-help">{schema.description}</div>}
      </div>
    );
  }
  if (widget === "embedding-provider-picker") {
    const err = (errors || []).find((e) => e.path === path);
    return renderInner(
      <div className="field">
        {schema.title && <label className="field-label">{schema.title}</label>}
        <HF_ProviderPicker endpoint="/v1/embedding_providers" value={value} onChange={onChange} label={schema.title || "Embedding provider"} />
        {err && <div className="field-help" style={{ color: "var(--red)" }}>{err.message}</div>}
        {schema.description && <div className="field-help">{schema.description}</div>}
      </div>
    );
  }
  if (widget === "cross-encoder-picker") {
    const err = (errors || []).find((e) => e.path === path);
    return renderInner(
      <div className="field">
        {schema.title && <label className="field-label">{schema.title}</label>}
        <HF_ProviderPicker endpoint="/v1/cross_encoder_providers" value={value} onChange={onChange} label={schema.title || "Cross-encoder"} />
        {err && <div className="field-help" style={{ color: "var(--red)" }}>{err.message}</div>}
        {schema.description && <div className="field-help">{schema.description}</div>}
      </div>
    );
  }
  if (widget === "ssp-picker") {
    const err = (errors || []).find((e) => e.path === path);
    return renderInner(
      <div className="field">
        {schema.title && <label className="field-label">{schema.title}</label>}
        <HF_ProviderPicker endpoint="/v1/ssp" value={value} onChange={onChange} label={schema.title || "SSP"} />
        {err && <div className="field-help" style={{ color: "var(--red)" }}>{err.message}</div>}
        {schema.description && <div className="field-help">{schema.description}</div>}
      </div>
    );
  }

  // enum → select
  if (schema.enum) {
    const err = (errors || []).find((e) => e.path === path);
    return renderInner(
      <div className="field">
        {schema.title && <label className="field-label">{schema.title}</label>}
        <select
          className="select"
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="">— select —</option>
          {schema.enum.map((v) => (
            <option key={String(v)} value={String(v)}>{String(v)}</option>
          ))}
        </select>
        {err && <div className="field-help" style={{ color: "var(--red)" }}>{err.message}</div>}
        {schema.description && <div className="field-help">{schema.description}</div>}
      </div>
    );
  }

  // boolean → checkbox
  if (type === "boolean") {
    return renderInner(
      <div className="field">
        <label className="field-label" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={!!value}
            onChange={(e) => onChange(e.target.checked)}
          />
          {schema.title || path.split(".").pop() || "value"}
        </label>
        {schema.description && <div className="field-help">{schema.description}</div>}
      </div>
    );
  }

  // number / integer → number input
  if (type === "number" || type === "integer") {
    const err = (errors || []).find((e) => e.path === path);
    return renderInner(
      <div className="field">
        {schema.title && <label className="field-label">{schema.title}</label>}
        <input
          type="number"
          className="input"
          value={value ?? ""}
          step={type === "integer" ? 1 : "any"}
          onChange={(e) => {
            const v = e.target.value;
            onChange(v === "" ? null : type === "integer" ? parseInt(v, 10) : parseFloat(v));
          }}
          style={{ width: "100%" }}
        />
        {err && <div className="field-help" style={{ color: "var(--red)" }}>{err.message}</div>}
        {schema.description && <div className="field-help">{schema.description}</div>}
      </div>
    );
  }

  // string → text input or textarea
  if (type === "string" || (!type && !schema.properties && !schema.items)) {
    const err = (errors || []).find((e) => e.path === path);
    const isTextarea = widget === "textarea";
    return renderInner(
      <div className="field">
        {schema.title && <label className="field-label">{schema.title}</label>}
        {isTextarea ? (
          <textarea
            className="textarea"
            rows={4}
            value={value ?? ""}
            onChange={(e) => onChange(e.target.value)}
          />
        ) : (
          <input
            type="text"
            className="input"
            value={value ?? ""}
            onChange={(e) => onChange(e.target.value)}
            style={{ width: "100%" }}
          />
        )}
        {err && <div className="field-help" style={{ color: "var(--red)" }}>{err.message}</div>}
        {schema.description && <div className="field-help">{schema.description}</div>}
      </div>
    );
  }

  // object → fieldset, recurse on properties
  if (type === "object" || schema.properties) {
    const props = schema.properties || {};
    const keys = Object.keys(props);
    const obj = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    return renderInner(
      <fieldset style={{ border: "1px solid var(--border)", borderRadius: 6, padding: "8px 12px", margin: "4px 0" }}>
        {schema.title && <legend style={{ fontSize: 12, color: "var(--text-2)", padding: "0 4px" }}>{schema.title}</legend>}
        {schema.description && <div className="field-help" style={{ marginBottom: 8 }}>{schema.description}</div>}
        {keys.map((k) => {
          const childPath = path ? path + "." + k : k;
          return (
            <JsonSchemaForm
              key={k}
              schema={{ ...props[k], title: props[k].title || k }}
              value={obj[k]}
              onChange={(v) => onChange({ ...obj, [k]: v })}
              errors={errors}
              _path={childPath}
            />
          );
        })}
        {keys.length === 0 && (
          <div className="muted text-sm">No properties defined.</div>
        )}
      </fieldset>
    );
  }

  // array → repeating item list with add/remove
  if (type === "array") {
    const items = Array.isArray(value) ? value : [];
    const itemSchema = schema.items || { type: "string" };
    return renderInner(
      <div className="field">
        {schema.title && <label className="field-label">{schema.title}</label>}
        {schema.description && <div className="field-help">{schema.description}</div>}
        {items.map((item, i) => (
          <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: 6, marginBottom: 4 }}>
            <div style={{ flex: 1 }}>
              <JsonSchemaForm
                schema={itemSchema}
                value={item}
                onChange={(v) => {
                  const next = [...items];
                  next[i] = v;
                  onChange(next);
                }}
                errors={errors}
                _path={path + "[" + i + "]"}
              />
            </div>
            <Btn
              size="sm"
              kind="ghost"
              icon="minus"
              onClick={() => {
                const next = items.filter((_, j) => j !== i);
                onChange(next);
              }}
              style={{ marginTop: 4, flexShrink: 0 }}
            >Remove</Btn>
          </div>
        ))}
        <Btn
          size="sm"
          kind="ghost"
          icon="plus"
          onClick={() => onChange([...items, null])}
        >Add item</Btn>
      </div>
    );
  }

  // Fallback — unknown type
  return renderInner(
    <div className="field">
      {schema.title && <label className="field-label">{schema.title}</label>}
      <pre className="code-block" style={{ fontSize: 11 }}>{JSON.stringify(value, null, 2)}</pre>
    </div>
  );
}

// ============================================================================
// validateSchema — minimal client-side validator
// Returns [{path, message}, ...]
// ============================================================================

function validateSchema(schema, value, _path) {
  if (!schema) return [];
  const path = _path || "";
  const errors = [];

  const type = schema.type;

  // required check (object level)
  if ((type === "object" || schema.properties) && schema.required && Array.isArray(schema.required)) {
    const obj = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    for (const k of schema.required) {
      const childPath = path ? path + "." + k : k;
      if (obj[k] == null || obj[k] === "") {
        errors.push({ path: childPath, message: `${k} is required` });
      }
    }
    // recurse on properties
    const props = schema.properties || {};
    for (const k of Object.keys(props)) {
      const childPath = path ? path + "." + k : k;
      const childErrors = validateSchema(props[k], obj[k], childPath);
      errors.push(...childErrors);
    }
  }

  // type check (leaf nodes)
  if (value != null && value !== "") {
    if (type === "string" && typeof value !== "string") {
      errors.push({ path, message: `Expected string` });
    }
    if (type === "number" && typeof value !== "number") {
      errors.push({ path, message: `Expected number` });
    }
    if (type === "integer" && (!Number.isInteger(value))) {
      errors.push({ path, message: `Expected integer` });
    }
    if (type === "boolean" && typeof value !== "boolean") {
      errors.push({ path, message: `Expected boolean` });
    }
    if (type === "array" && !Array.isArray(value)) {
      errors.push({ path, message: `Expected array` });
    }
    // array item validation
    if (type === "array" && Array.isArray(value) && schema.items) {
      value.forEach((item, i) => {
        const childErrors = validateSchema(schema.items, item, path + "[" + i + "]");
        errors.push(...childErrors);
      });
    }
  }

  return errors;
}

window.JsonSchemaForm = JsonSchemaForm;
window.validateSchema = validateSchema;
