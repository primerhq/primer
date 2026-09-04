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

// Designer reconciliation: "no raw snake_case labels where a human name
// is obvious" - the model-family classes' own /_types response already
// serves real labels ("Base URL", "API key (optional)", ...primer/api/
// routers/providers.py's _form_field calls), left untouched below; this
// is the fallback for the bare-string classes (web_search, web_fetch,
// speech - see the comment on PC_normalizeField) whose served "label" IS
// just the raw key, which PC_normalizeField's old `field.label || field.
// key` fallback rendered verbatim.
function PC_humanizeKey(key) {
  return String(key || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// _types answers in two shapes. web_search, web_fetch and the speech
// classes serve bare field NAMES (routers/web_search.py:216-222); the
// model-family classes serve DESCRIPTORS (routers/providers.py
// list_llm_provider_types). Normalise once so one renderer covers both.
function PC_normalizeField(field) {
  if (typeof field === "string") {
    return {
      key: field,
      label: PC_humanizeKey(field),
      type: PC_fieldType(field),
      required: false,
      help: "",
      options: null,
      placeholder: "",
    };
  }
  return {
    key: field.key,
    label: field.label || PC_humanizeKey(field.key),
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

function PC_Field({ field, value, onChange, disabled }) {
  let input;
  if (field.type === "model_list") {
    input = <PC_ModelListField value={value} onChange={onChange} />;
  } else if (field.type === "enum") {
    input = (
      <select value={value || ""} disabled={disabled}
        onChange={(e) => onChange(e.target.value)}>
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
        disabled={disabled}
        placeholder={field.placeholder}
        // 01a05198: on edit, a secret field's `value` is the served
        // MASK STRING verbatim (GET's own masked serialization - see
        // primer/model/common.py's _matches_served_mask) - unlike an
        // earlier iteration of this form, it is NOT blanked out. Left
        // untouched, the mask round-trips back on save exactly as
        // received; the backend's preserve_masked_secrets on_pre_update
        // hook recognizes that exact string and restores the real
        // credential instead of persisting the mask literally. Typing a
        // real replacement value submits that instead - it will not
        // match the mask pattern, so it passes through unchanged.
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  // Platform wave P1a item 4: secret fields (api_key/token/password/
  // secret_access_key - PC_fieldType above) get the reference's own label
  // pattern rather than an unlabeled password box. The value itself is
  // whatever is in `draft` already - on edit that is the served MASK
  // STRING verbatim (see the input's own comment above), so a masked
  // secret renders as "**********xxxx"-shaped text with no extra code
  // here: the mask IS the value, displayed the same as any other field.
  const isSecret = field.type === "password";
  return (
    <label className="field pc-field" data-field={field.key} data-locked={disabled ? "true" : "false"}>
      <span className="pc-field-label">
        <span className="pc-field-label-main">{field.label}</span>
        {isSecret ? (
          <span className="pc-field-label-annotation muted">secret — masked on read</span>
        ) : null}
        {field.required ? <em className="req"> *</em> : null}
      </span>
      {input}
      {field.help ? <span className="field-help">{field.help}</span> : null}
    </label>
  );
}

// Platform wave P1a item 4: "Live model probe" - a live POST against the
// CURRENT draft config (before Save), for kinds the served /_types data
// marks discoverable. Two REAL, confirmed backend endpoints exist for
// this shape (routers/providers.py): /llm_providers/_discover_models and
// /embedding_providers/_discover_models - other classes keep the existing
// generic runTest()/_test round trip below unchanged (PC_ProviderForm
// renders this panel ADDITIONALLY, not instead).
const PC_DISCOVER_MODELS_PLURALS = ["llm_providers", "embedding_providers"];

function PC_ProbePanel({ plural, draft, selectedType }) {
  const { apiFetch } = window.primerApi;
  const [busy, setBusy] = React.useState(false);
  const [result, setResult] = React.useState(null);

  const probe = async () => {
    setBusy(true);
    setResult(null);
    try {
      const out = await apiFetch("POST", `/${plural}/_discover_models`, {
        provider: selectedType,
        config: draft.config || {},
      });
      setResult({ ok: true, models: (out && out.models) || [] });
    } catch (err) {
      setResult({
        ok: false,
        error: (err && (err.detail || err.message)) || String(err),
      });
    } finally {
      setBusy(false);
    }
  };

  const models = (result && result.ok && result.models) || [];
  // Reference shows 5 named rows plus an overflow count for a 12-model
  // result; not a hard protocol limit, just the display cap.
  const shown = models.slice(0, 5);
  const overflow = models.length - shown.length;

  return (
    <div className="pc-probe" data-testid="provider-probe-panel">
      {/* Designer reconciliation: heading + compact green-outline Test
          connect on ONE row - kind="ghost" (transparent bg + border,
          the closest existing Btn variant to "outline") plus a scoped
          CSS override for the green tint, rather than a new Btn kind
          just for this one button. */}
      <div className="pc-probe-head-row">
        <div className="pc-probe-head">Live model probe</div>
        <Btn kind="ghost" size="sm" disabled={busy}
          data-testid="provider-probe-test" onClick={probe}>
          Test connect
        </Btn>
      </div>
      {result ? (
        result.ok ? (
          <div className="pc-probe-result" data-testid="provider-probe-result">
            <div className="pc-probe-count">
              {models.length} models · probed on the DRAFT config
            </div>
            <div className="pc-probe-models">
              {shown.map((m, i) => (
                <div key={i} className="pc-probe-model-row mono">{(m && m.name) || m}</div>
              ))}
            </div>
            {overflow > 0 ? (
              <div className="pc-probe-model-row muted text-sm">+ {overflow} more</div>
            ) : null}
          </div>
        ) : (
          <div className="field-help" data-testid="provider-probe-error">
            {result.error}
          </div>
        )
      ) : null}
    </div>
  );
}

// Lead ruling (designer reconciliation, Invalidate stop-and-flag): the
// card footer drops Invalidate to match the mockup exactly, but the
// capability itself is real and stays reachable - moved here, beside
// the panel above, as a deliberate capability-preserving addition the
// mockup itself doesn't show. Genuinely distinct from Test connect: that
// probes the DRAFT config pre-save, this drops the STORED adapter cache
// for the row that already exists (api/registries/provider_registry.py)
// - edit-mode only (nothing to invalidate before Save has ever run).
function PC_InvalidateAction({ plural, existingId }) {
  const { apiFetch } = window.primerApi;
  const [busy, setBusy] = React.useState(false);
  const [err, setErr] = React.useState("");

  const run = async () => {
    setErr("");
    setBusy(true);
    try {
      await apiFetch("POST", `/${plural}/${encodeURIComponent(existingId)}/invalidate`);
      const toast = window.primerApi && window.primerApi.toastPush;
      if (typeof toast === "function") {
        toast({ kind: "success", title: "Cache dropped", detail: existingId });
      }
    } catch (invalidateErr) {
      setErr((invalidateErr && (invalidateErr.detail || invalidateErr.message))
        || String(invalidateErr));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pc-invalidate" data-testid="provider-form-invalidate">
      <button type="button" className="pc-invalidate-link" disabled={busy}
        data-testid="provider-form-invalidate-action" onClick={run}>
        Invalidate model cache
      </button>
      {err ? (
        <span className="field-help" data-testid="provider-form-invalidate-error">{err}</span>
      ) : null}
    </div>
  );
}

// Platform wave P1a item 4: shared across every provider class's form
// modal (screenshots 5 and 7 both carry it verbatim) - explanatory copy
// about how /capabilities-driven install-wide defaults and aggregated
// failover chains work, not data fetched live from that endpoint.
const PC_CAPABILITIES_FOOTNOTE =
  "Speech, web-search and web-fetch families set the install-wide ACTIVE "
  + "config; aggregated kinds carry a failover chain.";

// Limits is required on every model-family provider row and
// max_concurrency inside it has no default, so the form has to offer it.
// The optional timeouts are left blank by default: an empty box means
// "use the model default", which is what the backend's None means.
// Designer reconciliation: the boxed <fieldset> dissolves into the same
// full-width, humanized-label fields every other field above/below it
// uses - a subtle group heading (not a border/legend box) is enough to
// keep the three fields visually together. Distinct labels for the two
// timeout fields (not a shared "Timeout (s)"): a provider row carries
// BOTH simultaneously (primer/model/providers/_shared.py), so one
// ambiguous label would leave an operator unable to tell which box sets
// which - the mockup's single "Timeout (s)" was for a kind (anthropic)
// whose real schema has no config-level timeout field at all, i.e. not
// a real precedent to generalize from (flagged in the PR body).
function PC_LimitsFieldset({ value, onChange }) {
  const limits = value || { max_concurrency: 1 };
  const set = (key, raw) => {
    const next = { ...limits };
    if (raw === "") delete next[key];
    else next[key] = Number(raw);
    onChange(next);
  };
  return (
    <div className="pc-limits-group" data-testid="provider-form-limits">
      <div className="pc-field-group-heading muted text-sm">Limits</div>
      <label className="field pc-field">
        <span className="pc-field-label">
          <span className="pc-field-label-main">Max concurrency</span>
          <em className="req"> *</em>
        </span>
        <input
          type="number"
          min="1"
          value={limits.max_concurrency == null ? 1 : limits.max_concurrency}
          onChange={(e) => set("max_concurrency", e.target.value || "1")}
        />
      </label>
      <label className="field pc-field">
        <span className="pc-field-label">
          <span className="pc-field-label-main">Request timeout (s)</span>
        </span>
        <input
          type="number"
          min="0"
          value={limits.request_timeout_seconds == null ? "" : limits.request_timeout_seconds}
          onChange={(e) => set("request_timeout_seconds", e.target.value)}
        />
      </label>
      <label className="field pc-field">
        <span className="pc-field-label">
          <span className="pc-field-label-main">Connect timeout (s)</span>
        </span>
        <input
          type="number"
          min="0"
          value={limits.connect_timeout_seconds == null ? "" : limits.connect_timeout_seconds}
          onChange={(e) => set("connect_timeout_seconds", e.target.value)}
        />
      </label>
    </div>
  );
}

// What the form shows is what it must send.
//
// PC_LimitsFieldset renders `value || { max_concurrency: 1 }`, a default
// that lives only in that component's render. An operator who fills the
// required fields and presses Save therefore sent no `limits` key at all,
// and every provider class declares limits as required: the create 422'd
// with "limits: Field required" while the form displayed a perfectly good
// max_concurrency of 1. Creating a provider through the catalog only
// worked if you happened to touch a Limits box first.
//
// Number inputs have the mirror problem. PC_Field stores e.target.value,
// which is a string, and an emptied box stores "". Both are rejected:
// "" fails float parsing, and a numeric string only survives because
// Pydantic coerces it. Send numbers as numbers and drop the empties.
function PC_submittable(draft, shape, selectedType) {
  const out = { ...draft };
  // The type dropdown has the same fault limits had: `selectedType` falls
  // back to the first key for DISPLAY, and that fallback was never written
  // to the draft. Saving without touching the dropdown therefore sent no
  // `provider` at all, which is required on every class, so the create
  // 422'd while the form showed a type selected.
  if (selectedType && !out.provider) out.provider = selectedType;
  if (shape && shape.limits) {
    out.limits = { max_concurrency: 1, ...(draft.limits || {}) };
  }
  const clean = (obj, fields) => {
    if (!obj) return obj;
    const next = { ...obj };
    (fields || []).forEach((f) => {
      const norm = PC_normalizeField(f);
      if (norm.type !== "number") return;
      const raw = next[norm.key];
      if (raw === "" || raw == null) delete next[norm.key];
      else if (typeof raw === "string") next[norm.key] = Number(raw);
    });
    return next;
  };
  const cleaned = clean(out, shape && shape.row_fields);
  cleaned.config = clean(out.config, shape && shape.config_fields);
  if (cleaned.config == null) delete cleaned.config;
  return cleaned;
}

function PC_ProviderForm({
  plural, typesPath, value, onChange, onSubmit, onTest, onCancel, editing,
  existingId, canInvalidate,
}) {
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

  // A model row seeded by "Add model" starts blank, and submitting one
  // sends models: [{}], which the API rejects with 422
  // body.models.0.name: Field required. The per-class create modals
  // gated Create on every row being filled; the catalog's form replaced
  // them with no gate at all, so the same bad request went out again.
  // A required field that is empty is the same fault as a blank model
  // row: the form knows the request cannot succeed and offers Save
  // anyway, so the operator learns what was missing from a 422 instead
  // of from the form. The marker is already rendered beside the label;
  // this makes it mean something.
  const missingRequired = () => {
    const blank = (v) => v === undefined || v === null
      || String(v).trim() === "";
    if (blank(draft.id)) return true;
    const check = (fields, scope) => (fields || []).some((f) => {
      const norm = PC_normalizeField(f);
      if (!norm.required) return false;
      const holder = scope === "config" ? (draft.config || {}) : draft;
      return blank(holder[norm.key]);
    });
    return check(shape.row_fields, "row") || check(shape.config_fields, "config");
  };

  const modelRowsIncomplete = (fields) =>
    (fields || []).some((f) => {
      const norm = PC_normalizeField(f);
      if (norm.type !== "model_list") return false;
      const rows = Array.isArray(draft[norm.key]) ? draft[norm.key] : [];
      return rows.some((r) => !String((r && r.name) || "").trim());
    });
  // _types answers a MAP keyed by provider-type value.
  const typeMap = types.data || {};
  const typeKeys = Object.keys(typeMap);
  const selectedType = draft.provider || typeKeys[0] || "";
  const shape = typeMap[selectedType] || {};

  // 01a05198: no strip-on-edit effect needed any more - a secret field's
  // draft value on edit is simply whatever GET served (the mask
  // string), left as-is. Untouched, it submits verbatim and the
  // backend's preserve_masked_secrets on_pre_update hook restores the
  // real credential (primer/model/common.py); typed over, the new value
  // submits and passes through unchanged (it won't match the mask
  // pattern). This is what makes the required-field gate above safe to
  // read literally again: a masked value is never blank, so a
  // schema-required secret left untouched no longer looks "missing."

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
        ? await onTest(PC_submittable(draft, shape, selectedType))
        : await apiFetch("POST", `/${plural}/_test`,
                         PC_submittable(draft, shape, selectedType));
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

  // Platform wave P1a item 4: the rich "Live model probe" panel is
  // ADDITIONAL, not a replacement for the generic Test button below -
  // gated on the served /_types discoverable flag for THIS kind (not a
  // hardcoded kind list) and on a confirmed real endpoint existing for
  // this class (see PC_DISCOVER_MODELS_PLURALS's own comment).
  const showProbePanel = !!shape.discoverable
    && PC_DISCOVER_MODELS_PLURALS.indexOf(plural) >= 0;

  // Designer reconciliation: the in-form kind dropdown (and the T0379
  // mismatch warning it existed to explain) is REMOVED - kind arrives
  // preselected from the catalog's own kind-listing Register dropdown
  // (create) or is simply the row's own real value (edit), so
  // `selectedType`/`shape` above are never actually driven by a control
  // in this form any more, only read from `draft.provider`. The T0379
  // mismatch class this warning existed for mostly evaporates once kind
  // can no longer be picked independently of the fields shown for it -
  // flagged in the PR body per the task's own instruction.
  const fields = (
    <div className="col" style={{ gap: 12, minWidth: 0 }}>
      {missingExtra ? (
        <Banner kind="info" title="Optional dependency required">
          {capabilityHint(extra)}
        </Banner>
      ) : null}

      <PC_Field
        field={PC_normalizeField({
          key: "id", label: "Name", required: true,
          placeholder: selectedType ? `e.g. ${selectedType}-prod` : "e.g. my-provider",
        })}
        value={draft.id}
        disabled={editing}
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
    </div>
  );

  // Invalidate is edit-mode-only (nothing to invalidate before Save has
  // ever run) and class-gated (canInvalidate, from the catalog's own
  // klass.invalidate flag - llm/embedding/cross_encoder only). It needs
  // a right column even for a kind/class with no probe panel (cross_
  // encoder has no discover_models route at all, so showProbePanel is
  // always false for it, but it DOES carry klass.invalidate) - the two
  // gates are independent, so the column renders whenever EITHER wants it.
  const showInvalidate = editing && canInvalidate && !!existingId;
  const showRightColumn = showProbePanel || showInvalidate;

  return (
    <div className="col" style={{ gap: 12 }}
      data-testid={`provider-form-${plural}`}>
      {/* 01a04d6a: was a flex row (fields flex:1 vs. the probe panel's
          fixed 240px) inside the default 420px Modal - the fields
          column computed to a sliver once the probe panel's width and
          the 20px gap were subtracted (S3 screenshot). A real grid with
          an explicit fields column, sized against the WIDER modal this
          form now opens in (ProviderCatalog passes width={720}), fixes
          both the immediate squeeze and any future modal-width drift -
          the column proportions are declared here, not fought over with
          a competing fixed-width sibling. minmax(0, 1fr) - not the bare
          1fr shorthand - so a long unbreakable value (a pasted URL/key)
          cannot blow the fields column past its share (grid's default
          auto minimum forces content-based sizing without it). */}
      <div className="pc-form-grid" style={{
        display: "grid",
        gridTemplateColumns: showRightColumn ? "minmax(0, 1fr) 240px" : "minmax(0, 1fr)",
        gap: 20, alignItems: "start",
      }}>
        {fields}
        {showRightColumn ? (
          <div className="col pc-form-right" style={{ gap: 10, minWidth: 0 }}>
            {showProbePanel ? (
              <React.Fragment>
                <PC_ProbePanel plural={plural} draft={draft} selectedType={selectedType} />
                <div className="field-help" data-testid="provider-form-capabilities-footnote">
                  {PC_CAPABILITIES_FOOTNOTE}
                </div>
              </React.Fragment>
            ) : null}
            {showInvalidate ? (
              <PC_InvalidateAction plural={plural} existingId={existingId} />
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
        {typeof onCancel === "function" ? (
          <Btn kind="ghost" data-testid="provider-form-cancel" onClick={onCancel}>
            Cancel
          </Btn>
        ) : null}
        {/* Designer reconciliation: the footer Test button is redundant
            ONLY where the probe panel already offers Test connect - for
            classes with no probe panel (no discover_models route: stt/
            tts/web_search/web_fetch/artifact_storage/cross_encoder),
            removing it would leave NO way at all to test a draft config
            before saving, which the task's own tripwire says to flag
            rather than drop. Kept, conditionally, for exactly those. */}
        {!showProbePanel ? (
          <Btn
            kind="ghost"
            data-testid="provider-form-test"
            onClick={runTest}
            disabled={busy}
          >
            Test
          </Btn>
        ) : null}
        <Btn data-testid="provider-form-save"
          onClick={() => onSubmit && onSubmit(PC_submittable(draft, shape, selectedType))}
          disabled={busy || missingRequired()
            || modelRowsIncomplete(shape.row_fields)}>
          Save provider
        </Btn>
      </div>
    </div>
  );
}

window.PC_FIELD_TYPES = PC_FIELD_TYPES;
window.PC_normalizeField = PC_normalizeField;
window.PC_submittable = PC_submittable;
window.PC_ProviderForm = PC_ProviderForm;
