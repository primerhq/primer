/* global React, Icon, Btn */
// JsonSchemaForm — recursive JSON-Schema renderer used by HarnessRegisterDialog
// (overrides form). Prefix HF_ to avoid global name collisions.
//
// Composite-override note (Spec A §13): when the schema being rendered has a
// property literally named "dependencies" whose value is an object with its
// own `properties`, that block is rendered as a vertical stack of collapsible
// cards (one per dep-name) rather than as a generic nested fieldset. Each
// card recurses back through JsonSchemaForm with the dep's own sub-schema.

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
// Custom widget: combined LLM provider + model picker
// ============================================================================
//
// value is an object { provider_id, model_name }. Picking a provider populates
// the model dropdown from that provider's own model list (already carried on
// each /v1/llm_providers row) and defaults to its first model, so the operator
// never types a model name.

function HF_LlmModelPicker({ value, onChange, label }) {
  const { useResource, apiFetch } = window.primerApi;
  const res = useResource(
    "hf-llm-model-picker:/v1/llm_providers",
    (signal) => apiFetch("GET", "/v1/llm_providers?limit=200", null, { signal }),
    { pollMs: null }
  );
  const providers = res.data?.items ?? [];
  const v = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const providerId = v.provider_id ?? "";
  const modelName = v.model_name ?? "";

  const modelsFor = (pid) =>
    (providers.find((p) => p.id === pid)?.models || []).map((m) => m.name);
  const models = modelsFor(providerId);

  const onProvider = (pid) => {
    if (!pid) {
      onChange(null);
      return;
    }
    const ms = modelsFor(pid);
    // Default to the provider's first model the moment it is selected.
    onChange({ provider_id: pid, model_name: ms.length ? ms[0] : "" });
  };

  // Keep a saved model that is no longer in the provider's list visible so it
  // is not silently dropped when editing an existing binding.
  const modelOptions =
    modelName && !models.includes(modelName) ? [modelName, ...models] : models;

  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      <select
        className="select"
        style={{ flex: "1 1 180px", minWidth: 0 }}
        value={providerId}
        onChange={(e) => onProvider(e.target.value)}
      >
        <option value="">Select {label}</option>
        {providers.map((p) => (
          <option key={p.id} value={p.id}>{p.id}</option>
        ))}
      </select>
      <select
        className="select"
        style={{ flex: "1 1 180px", minWidth: 0 }}
        value={modelName}
        disabled={!providerId}
        onChange={(e) =>
          onChange({ provider_id: providerId, model_name: e.target.value })
        }
      >
        {modelOptions.length === 0 ? (
          <option value="">
            {providerId ? "No models on this provider" : "Select a provider first"}
          </option>
        ) : (
          modelOptions.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))
        )}
      </select>
    </div>
  );
}

// ============================================================================
// HF_DepCard — collapsible card for one entry in a `dependencies` block
// ============================================================================

function HF_DepCard({ depName, schema, value, onChange, errors, path }) {
  const [collapsed, setCollapsed] = React.useState(false);
  const header = (
    <div
      className="panel-h"
      onClick={() => setCollapsed((c) => !c)}
      style={{ cursor: "pointer", userSelect: "none" }}
      title={collapsed ? "Expand" : "Collapse"}
    >
      <Icon name={collapsed ? "chevron-right" : "chevron-down"} size={13} />
      <span className="mono" style={{ fontWeight: 600 }}>{depName}</span>
      {schema?.title && schema.title !== depName && (
        <span className="muted text-sm" style={{ marginLeft: 6 }}>{schema.title}</span>
      )}
    </div>
  );
  return (
    <div
      className="panel"
      data-testid={`dep-card-${depName}`}
      style={{ margin: "6px 0" }}
    >
      {header}
      {!collapsed && (
        <div className="panel-body" style={{ padding: "8px 12px" }}>
          {schema?.description && (
            <div className="field-help" style={{ marginBottom: 6 }}>{schema.description}</div>
          )}
          <JsonSchemaForm
            schema={{ ...schema, title: undefined, description: undefined }}
            value={value}
            onChange={onChange}
            errors={errors}
            _path={path}
          />
        </div>
      )}
    </div>
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
  if (widget === "llm-model-picker") {
    const err = (errors || []).find((e) => e.path === path);
    return renderInner(
      <div className="field">
        {schema.title && <label className="field-label">{schema.title}</label>}
        <HF_LlmModelPicker value={value} onChange={onChange} label={schema.title || "LLM provider"} />
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

    // Composite-override special case (Spec A §13):
    // A property literally named "dependencies" whose value is an object with
    // its own `properties` map (one key per dep-name) — render the children
    // as collapsible cards instead of plain nested fieldsets.
    const HF_renderDependenciesBlock = (depsSchema, depsValue, depsOnChange, depsPath) => {
      const depProps = depsSchema.properties || {};
      const depKeys = Object.keys(depProps);
      const depObj = depsValue && typeof depsValue === "object" && !Array.isArray(depsValue) ? depsValue : {};
      return (
        <div data-testid="dep-cards-block" style={{ display: "flex", flexDirection: "column", gap: 0 }}>
          {depKeys.length === 0 && (
            <div className="muted text-sm">No dependencies declared.</div>
          )}
          {depKeys.map((depName) => {
            const childPath = depsPath ? depsPath + "." + depName : depName;
            return (
              <HF_DepCard
                key={depName}
                depName={depName}
                schema={depProps[depName]}
                value={depObj[depName]}
                onChange={(v) => depsOnChange({ ...depObj, [depName]: v })}
                errors={errors}
                path={childPath}
              />
            );
          })}
        </div>
      );
    };

    return renderInner(
      <fieldset style={{ border: "1px solid var(--border)", borderRadius: 6, padding: "8px 12px", margin: "4px 0" }}>
        {schema.title && <legend style={{ fontSize: 12, color: "var(--text-2)", padding: "0 4px" }}>{schema.title}</legend>}
        {schema.description && <div className="field-help" style={{ marginBottom: 8 }}>{schema.description}</div>}
        {keys.map((k) => {
          const childPath = path ? path + "." + k : k;
          const childSchema = props[k];
          const isDeps = k === "dependencies"
            && childSchema
            && (childSchema.type === "object" || childSchema.properties)
            && childSchema.properties
            && typeof childSchema.properties === "object";
          if (isDeps) {
            return (
              <div key={k} className="field">
                <label className="field-label">{childSchema.title || "Dependencies"}</label>
                {childSchema.description && (
                  <div className="field-help" style={{ marginBottom: 6 }}>{childSchema.description}</div>
                )}
                {HF_renderDependenciesBlock(
                  childSchema,
                  obj[k],
                  (v) => onChange({ ...obj, [k]: v }),
                  childPath,
                )}
              </div>
            );
          }
          return (
            <JsonSchemaForm
              key={k}
              schema={{ ...childSchema, title: childSchema.title || k }}
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
